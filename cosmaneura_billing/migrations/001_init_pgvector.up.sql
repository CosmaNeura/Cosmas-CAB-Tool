-- 001_init_pgvector.up.sql
-- Dialogue-RAG vector store for CosmaNeura (replaces Pinecone).
-- Target: PostgreSQL 14+ with the `vector` extension (pgvector >= 0.7 for HNSW).
--
-- Embedding dimension MUST match your embedding model:
--   OpenAI text-embedding-3-small ...... 1536   <- default here
--   OpenAI text-embedding-ada-002 ...... 1536   (prototype model; same size)
--   OpenAI text-embedding-3-large ...... 3072   (change vector(1536) -> vector(3072))
-- If you change models later, you must re-embed and recreate the column/index.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- Patient-doctor dialogue chunks (e.g. iCliniq). `id` matches the loader's
-- f"icliniq_{i:06d}" ids. `meta` holds the clinician answer + source for
-- grounding the leading-questions prompt; PgVectorStore reads/writes these.
CREATE TABLE IF NOT EXISTS dialog_chunks (
    id          text PRIMARY KEY,
    embedding   vector(1536) NOT NULL,
    meta        jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz  NOT NULL DEFAULT now()
);

-- Approximate-nearest-neighbor index for cosine distance (matches the
-- `embedding <=> $1` operator used by PgVectorStore.query).
-- HNSW gives low-latency ANN; tune m / ef_construction for your corpus size.
CREATE INDEX IF NOT EXISTS dialog_chunks_embedding_hnsw
    ON dialog_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Optional: filter dialogues by metadata fields (e.g. source/topic).
CREATE INDEX IF NOT EXISTS dialog_chunks_meta_gin
    ON dialog_chunks USING gin (meta jsonb_path_ops);

COMMIT;

-- Query-time recall/speed knob (set per session/connection as needed):
--   SET hnsw.ef_search = 80;   -- higher = better recall, slower
