-- 001_init_pgvector.down.sql
-- Rollback for 001_init_pgvector.up.sql.
-- Drops the dialogue table and its indexes. The `vector` extension is left
-- installed (other tables may use it); drop it manually if truly unused:
--   DROP EXTENSION IF EXISTS vector;

BEGIN;

DROP INDEX IF EXISTS dialog_chunks_meta_gin;
DROP INDEX IF EXISTS dialog_chunks_embedding_hnsw;
DROP TABLE IF EXISTS dialog_chunks;

COMMIT;
