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

`007_synthetic_demo_release_candidate_source` adds one explicit encounter
source value for the fixed, repository-owned synthetic demo seed. It does not
insert a chart or broaden any runtime privilege. The matching down migration
restores the prior allowlist and therefore fails closed while such a seed row
still exists.

`008_synthetic_demo_encounter_immutability` installs invoker-security,
`ENABLE ALWAYS` guards that compare `current_user` with the exact `encounters`
table owner. Non-owner roles cannot insert the reserved source, update either
from or to it, delete reserved rows, or truncate the table even if a future
grant accidentally gives them that table authority. The table-owning migration
identity retains seed and explicit cleanup authority. The provisioned runtime
and worker roles also have no `TRIGGER` privilege, no `CREATE` on the `public`
schema, and no `SET` privilege on `session_replication_role`. The migration
changes no row, RLS policy, or table grant.

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

Other managed Postgres services enable `pgvector` similarly through their
extension allowlists; the migration itself is identical. Do not infer compliance
coverage from cloud ownership alone. The selected database service, region,
configuration, and every service that receives source text must each be covered
by the applicable executed agreement/BAA and approved data-handling posture.
Keeping vectors in the platform database removes a separate vector-database
vendor, but does not authorize an embedding-model disclosure.

## Embedding dimension

`vector(1536)` matches the historical development embedding seam. The current
`OpenAIEmbedder` calls direct `api.openai.com`; it is not part of the Azure pilot
runtime and is prohibited in staging and production. A future Azure OpenAI
embedding migration must pin an Azure model deployment, confirm its output
dimension, update this migration if needed, re-embed the corpus, and recreate
the index. The live clinical-extraction deployment is not an embedding model.

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

### Fixed synthetic demo seed

After migrations `007` and `008` are applied, a reviewed synthetic demo may
load the twelve digest-pinned cases from the base and source-extension packs
through the same private migration execution environment:

    DATABASE_URL='<migration-owner DSN>' \
    COSMANEURA_ENTRA_TENANT_ID='<tenant UUID>' \
    COSMANEURA_PRACTICE_ID='<stable practice ID>' \
    python -m cosmaneura_billing.seed_demo_release_candidate

The command accepts no arguments. It validates both packaged packs, every fixed
case ID and digest, and the combined seed digest; proves it is using the exact
table owner with both exact `ENABLE ALWAYS` migration-008 guard shapes and the
expected invoker-security function owner/search path; proves the practice/tenant
binding; inserts without an update path; and treats only byte-equivalent
persisted fields as an idempotent replay. Direct SQL under runtime or worker
roles cannot claim or mutate the reserved source.
Use the Container Apps override and acceptance sequence in `../../infra/README.md`
for Azure; never paste the owner DSN into a shell command there.

### Manual SQL (disposable/reviewed use only)

Plain psql:

    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init_pgvector.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_chase_persistence.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_pilot_persistence.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_chase_delivery_runtime.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_accepted_delivery_attempt.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_chase_policy_revocation.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_synthetic_demo_release_candidate_source.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/008_synthetic_demo_encounter_immutability.up.sql
    # rollback:
    # PRECONDITION: close API, worker, and sync ingress/scheduling. As the exact
    # table owner, select each bound practice via cosmaneura.practice_id, delete
    # its reserved-source rows, and verify that practice has zero remaining.
    # Repeat for every bound practice; forced RLS makes an unscoped count unsafe.
    # Keep ingress closed until both 008 and 007 are down.
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/008_synthetic_demo_encounter_immutability.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_synthetic_demo_release_candidate_source.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_chase_policy_revocation.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_accepted_delivery_attempt.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_chase_delivery_runtime.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_pilot_persistence.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_chase_persistence.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init_pgvector.down.sql

The down migrations are destructive and are intended only for an explicitly
selected disposable database or a reviewed rollback. Migration `008` cannot
perform a reliable cross-practice row check through forced RLS, so its stated
closed-ingress, per-practice owner cleanup is a mandatory operational gate, not
an optional verification. `002` is safe to apply before `001` when the chase
store is needed before the dialogue index. Direct
`psql` does not provision the API/worker runtime roles or bootstrap
administrator, so it is not a substitute for the deployment runner.

The corpus indexer (`scripts/index_dialogues.py`) is a dormant development seam,
not a pilot deployment step. Its direct `api.openai.com` mode must not be enabled
in staging or production and must never receive real patient text. Keep
`OPENAI_API_KEY` unset in deployed environments. Before this path can become a
pilot feature it must be separately migrated to private Azure OpenAI,
managed-identity authenticated, included in the cost and subprocessor inventory,
and accepted under the same PHI controls as clinical extraction.

If you adopt a migration tool, drop these in as the first version:

- Flyway: rename to `V1__init_pgvector.sql` (forward only) in your migrations dir.
- Alembic: in a version file, run the SQL via `op.execute(open(...).read())`, or
  paste the statements into `upgrade()` / `downgrade()`.
- Django: equivalent to a `VectorExtension()` op + a `CREATE TABLE`; or run the SQL
  in a `RunSQL` migration.

## Note on PHI

If patient utterances that reach `dialog_chunks` can contain PHI, this database is
in scope for HIPAA: encrypt at rest and in transit, restrict access, enable audit
logging, and include it in the service/subprocessor/BAA inventory. The current
pilot does not authorize PHI in this table or any external embedding call.
Minimizing and properly de-identifying indexed text keeps that surface small.
