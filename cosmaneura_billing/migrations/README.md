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

## Requirements

- PostgreSQL 14 or newer.
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

Plain psql:

    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init_pgvector.up.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_chase_persistence.up.sql
    # rollback:
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_chase_persistence.down.sql
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init_pgvector.down.sql

The down migrations are destructive and are intended only for an explicitly
selected disposable database or a reviewed rollback. `002` is safe to apply
before `001` when the chase store is needed before the dialogue index.

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
