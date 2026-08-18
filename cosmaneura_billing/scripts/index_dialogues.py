#!/usr/bin/env python3
"""
Index a dialogue corpus into pgvector (production ingestion for the leading-
questions RAG). This is the Pinecone-free path: vectors live in your own
Postgres, under your existing cloud BAA.

One-time DB setup (psql):
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE dialog_chunks (id text PRIMARY KEY, embedding vector(1536), meta jsonb);
    CREATE INDEX ON dialog_chunks USING hnsw (embedding vector_cosine_ops);

Run:
    export OPENAI_API_KEY=...                # ZDR + BAA project for PHI
    export DATABASE_URL=postgresql://user:pass@host:5432/db
    python scripts/index_dialogues.py corpus.jsonl

corpus.jsonl: one JSON object per line, e.g.
    {"id": "icliniq_001", "text": "Patient reports ...", "topic": "gi"}
Or pass the prototype's iCliniq corpus directly:
    python scripts/index_dialogues.py iCliniq.feather        # or med_chat_QA_data.csv

This script is intentionally dependency-light; install psycopg[binary] to run it.
For local development without a database, use dialogue_rag.py (LocalVectorStore).
"""
import json
import os
import sys

# allow running as `python scripts/index_dialogues.py` from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cosmaneura_billing.corpus import load_icliniq
from cosmaneura_billing.dialogue_rag import DialogueRAG, MockEmbedder, OpenAIEmbedder
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.vectorstore import PgVectorStore


def load_corpus(path: str) -> list[dict]:
    lower = path.lower()
    if lower.endswith((".feather", ".csv")):
        return load_icliniq(path)            # iCliniq feather/CSV -> {id, text, answer, source}
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    corpus = load_corpus(sys.argv[1])

    # Embedder: real in prod (BAA + ZDR), mock if no key (lets you dry-run the path).
    embedder = (
        OpenAIEmbedder(settings=settings)
        if settings.openai_api_key else MockEmbedder(dim=1536)
    )

    dsn = settings.database_url
    if not dsn:
        print("Set DATABASE_URL to upsert into pgvector. Aborting (no DB configured).")
        return 1

    import psycopg  # psycopg 3
    with psycopg.connect(dsn) as conn:
        store = PgVectorStore(conn, table="dialog_chunks")
        rag = DialogueRAG(embedder=embedder, store=store)
        n = rag.index(corpus)
        print(f"indexed {n} dialogue chunks into pgvector (table dialog_chunks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
