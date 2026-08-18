# Database migrations

These migrations provision the platform-owned PostgreSQL state. They are plain,
ordered SQL so they can run from `psql` now and move behind a migration runner
without rewriting the schema later.

## What it provisions

`001_init_pgvector` enables the `vector` extension and creates `dialog_chunks`
(`id text`, `embedding vector(1536)`, `meta jsonb`, `created_at`), plus an HNSW
cosine index and a GIN index on `meta`. The table and operators line up exactly
with `cosmaneura_billing/vectorstore.py::PgVectorStore`.

`002_chase_persistence` creates practice-scoped chase items, append-only contact
attempts, pending staff tasks, and resolutions. Composite keys and foreign keys
include `practice_id`; the application also applies that scope to every query.
The chase migration does not require pgvector and may be run independently.

`003_pilot_persistence` makes PostgreSQL the pilot system of record for
practices, explicit Entra practice memberships, encounters, normalized coder
feedback and access-audit attempts/completions. It brings the chase tables under
the same practice foreign key and forced row-level-security boundary. Staging
and production require this store; JSONL feedback/audit files and in-memory
encounters are local development/test seams only.

`004_chase_delivery_runtime` adds the durable chase scheduler: verified contact
snapshots and routes, immutable policy revisions and steps, Admin approval and
revocation events, exact per-message and direct-contract review releases,
active policy/cadence state, and a leased delivery outbox with append-only
events. Its constraints and forced RLS bind every executable touch to the exact
approved policy, contact, gate, and practice evidence.

`005_accepted_delivery_attempt` adds one narrowly scoped security-definer
function. The worker may call it only for an already-finalized accepted outbox
row; the function derives and atomically appends the corresponding system
contact attempt. The worker never receives direct write authority over staff
contact history.

`006_chase_policy_revocation` safely retires queued, no-provider-call work when
an exact policy is revoked or gate evidence changes. It permits a later exact
approval to replace only cancelled or definitive-failure reservations;
submitting, accepted, and ambiguous deliveries remain uniqueness barriers and
are never made retryable.

## Requirements

- PostgreSQL 16 for the pilot persistence schema (`003`).
- PostgreSQL 14 or newer is sufficient only for the earlier standalone
  dialogue/chase migrations.
- The `pgvector` extension enabled on the server (>= 0.7.0 for HNSW) only when
  running `001_init_pgvector`.

### Azure Database for PostgreSQL — Flexible Server (your choice)

On Azure, `pgvector` ships with the server but must be **allow-listed** before you
can `CREATE EXTENSION`. Add `vector` to the `azure.extensions` server parameter,
then create the extension:

1. Allow-list the extension (Portal: *Server parameters* → search `azure.extensions`
   → check `VECTOR` → Save), or with the CLI:

       az postgres flexible-server parameter set \
         --resource-group <rg> --server-name <server> \
         --name azure.extensions --value VECTOR

   (If your tier requires it, the change may prompt a restart.)
2. Connect to the database and run the migration (it issues `CREATE EXTENSION IF
   NOT EXISTS vector` plus the table/indexes):

       psql "host=<server>.postgres.database.azure.com port=5432 dbname=<db> \
             user=<user> sslmode=require" -f migrations/001_init_pgvector.up.sql

3. Use a recent Flexible Server Postgres version (16/17) so the bundled `pgvector`
   supports HNSW. If `CREATE INDEX ... USING hnsw` errors, your `pgvector` predates
   0.5.0 — upgrade the server version, or temporarily switch the index to `ivfflat`.

Connection string for the indexer (note `sslmode=require`, which Azure enforces):

    export DATABASE_URL="postgresql://<user>:<pass>@<server>.postgres.database.azure.com:5432/<db>?sslmode=require"

Other managed Postgres (AWS RDS/Aurora, Google Cloud SQL) enable `pgvector`
similarly via their extension-allowlist setting; the migration itself is identical.
Whichever you use runs inside your own cloud account under your existing cloud BAA,
so there's no separate vector-DB vendor or BAA (this is the Pinecone we removed).

## Embedding dimension

`vector(1536)` matches OpenAI `text-embedding-3-small` (and the prototype's
`ada-002`). If you ever move to `text-embedding-3-large`, change `1536` to `3072`
in the up-migration, re-embed the corpus, and recreate the index.

## Running it

### Deployment runner (required for the pilot)

Use the locked runner from the one-shot migration job:

    DATABASE_URL='<migration-owner DSN>' \
    COSMANEURA_DB_RUNTIME_ROLE=cosmaneura_app \
    COSMANEURA_DB_RUNTIME_PASSWORD='<separate runtime password>' \
    COSMANEURA_CHASE_WORKER_DB_ROLE=cosmaneura_chase_worker \
    COSMANEURA_CHASE_WORKER_DB_PASSWORD='<separate worker password>' \
    COSMANEURA_ENTRA_TENANT_ID='<tenant UUID>' \
    COSMANEURA_PRACTICE_ID='<stable practice ID>' \
    COSMANEURA_PRACTICE_NAME='<reviewed display name>' \
    COSMANEURA_BOOTSTRAP_ADMIN_OID='<immutable Entra object UUID>' \
    python -m cosmaneura_billing.migrations

The runner serializes concurrent executions, applies each migration and its
ledger row in one transaction, and refuses to continue if an already-applied
file's SHA-256 changes. It also creates or rotates the least-privilege API and
chase-worker logins, binds each login to exactly one practice, and grants only
the required table/sequence/function privileges. The bootstrap OID creates the
initial reviewed `Admin` membership once; later migration runs never reactivate
or overwrite a disabled membership. It does not discover users or create
memberships from login traffic.

Keep three credentials:

- The migration owner can change schema, provision the two runtime logins, and
  run the explicit membership command. It is available only to controlled jobs
  or operators and is never passed to the API container.
- The API's `DATABASE_URL` uses `COSMANEURA_DB_RUNTIME_ROLE` (normally
  `cosmaneura_app`). That role is `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`,
  `NOINHERIT`, and `NOBYPASSRLS`; it cannot update or delete audit rows. Forced
  RLS requires its practice setting to match its protected runtime-role
  binding, so changing the setting cannot expose another practice.
- The one-shot chase worker uses `COSMANEURA_CHASE_WORKER_DATABASE_URL` with
  `COSMANEURA_CHASE_WORKER_DB_ROLE` (normally `cosmaneura_chase_worker`). It can
  read immutable chase authorization evidence and write only scheduler effects;
  it cannot read identity, encounter, feedback, or access-audit tables and
  cannot create policies, approvals, or human releases. Never pass this DSN,
  its password, or the ACS connection string to the API. The API and worker
  instead receive identical non-credential ACS endpoint/sender/reply metadata
  and the shared chase-gate key needed to compute the same exact evidence.

The Azure deployment must store these as separate Key Vault secrets and expose
each only to its owning job or service. Never paste a real DSN or password into
a tracked file, command history, or support output.

### Explicit membership administration

After the first administrator is bootstrapped, add, change, or disable every
practice member explicitly with the migration-owner DSN from a private,
controlled execution environment:

    DATABASE_URL='<migration-owner DSN>' \
    COSMANEURA_ENTRA_TENANT_ID='<tenant UUID>' \
    COSMANEURA_PRACTICE_ID='<stable practice ID>' \
    python -m cosmaneura_billing.memberships \
      '<immutable Entra object UUID>' --role Coder

Repeat `--role` for multiple reviewed roles. To disable a member, pass
`--disable` plus the member's reviewed role set. The CLI validates the practice
and tenant binding and never accepts a display name or email as identity. There
is no just-in-time membership path: an Entra login without an active database
membership remains denied.

### Manual SQL (disposable/reviewed use only)

Plain psql:

    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init_pgvector.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_chase_persistence.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_pilot_persistence.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_chase_delivery_runtime.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_accepted_delivery_attempt.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_chase_policy_revocation.up.sql
    # rollback:
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_chase_policy_revocation.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_accepted_delivery_attempt.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_chase_delivery_runtime.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_pilot_persistence.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_chase_persistence.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init_pgvector.down.sql

The down migrations are destructive and are intended only for an explicitly
selected disposable database or a reviewed rollback. `002` is safe to apply
before `001` when the chase store is needed before the dialogue index. Direct
`psql` does not provision the API/worker runtime roles or bootstrap
administrator, so it is not a substitute for the deployment runner.

Then load the corpus (see scripts/index_dialogues.py):

    export DATABASE_URL=postgresql://user:pass@host:5432/db
    export OPENAI_API_KEY=...        # ZDR + BAA project for PHI
    python scripts/index_dialogues.py iCliniq.feather

If you adopt a migration tool, drop these in as the first version:

- Flyway: rename to `V1__init_pgvector.sql` (forward only) in your migrations dir.
- Alembic: in a version file, run the SQL via `op.execute(open(...).read())`, or
  paste the statements into `upgrade()` / `downgrade()`.
- Django: equivalent to a `VectorExtension()` op + a `CREATE TABLE`; or run the SQL
  in a `RunSQL` migration.

## Note on PHI

If patient utterances that reach `dialog_chunks` can contain PHI, this database is
in scope for HIPAA: encrypt at rest and in transit, restrict access, enable audit
logging, and include it in your subprocessor/BAA inventory. Minimizing PHI in the
indexed text (de-identify where feasible) keeps that surface small.
