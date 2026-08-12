"""
Vector store — a drop-in replacement for the Pinecone dependency.

Why this exists
---------------
Pinecone is a *managed* approximate-nearest-neighbor service. In the prototype it
was used in exactly one place: the "leading questions" dialogue retrieval
(feature3), which indexes a corpus of patient-doctor dialogues. The CCSR
similarity search (feature2/feature5) never used Pinecone at all — it is an
in-memory cosine computation over 547 category vectors (see
``knowledge.icd10_ccsr.embed_search``). The billing engine in this package uses
no vector database whatsoever.

Nearest-neighbor search is a commodity. This module gives you the same
upsert/query surface Pinecone exposes, backed by code that runs inside your own
infrastructure (so it is covered by your existing cloud BAA and adds no new
subprocessor and no separate Pinecone contract):

  * ``LocalVectorStore`` — pure-numpy exact cosine search. Zero dependencies
    beyond numpy, runs anywhere, fine up to ~10^5–10^6 vectors. Optional FAISS
    backend (``backend="faiss"``) for larger corpora / ANN speed.
  * For production we recommend ``pgvector`` (a Postgres extension): your vectors
    live in the database you already run, under one BAA, with SQL filtering. The
    ``PgVectorStore`` adapter below shows the shape — wire it to your DB driver.

Swapping from Pinecone is mechanical because the method names match:
    index.upsert(items)         # Pinecone: index.upsert(vectors=...)
    index.query(vec, top_k=5)   # Pinecone: index.query(vector=..., top_k=...)

Self-test:  python -m cosmaneura_billing.vectorstore
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol


@dataclass
class Match:
    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    def upsert(self, items: Iterable[tuple[str, list[float], dict]]) -> int: ...
    def query(self, vector: list[float], top_k: int = 5) -> list[Match]: ...
    def delete(self, ids: Iterable[str]) -> int: ...


class LocalVectorStore:
    """Exact cosine search in-process. numpy-only by default; optional FAISS."""

    def __init__(self, dim: int | None = None, backend: str = "numpy"):
        import numpy as np
        self._np = np
        self.dim = dim
        self.backend = backend
        self._ids: list[str] = []
        self._meta: list[dict] = []
        self._mat = None          # numpy array (n, dim), L2-normalized
        self._faiss_index = None

    # ---- write ---------------------------------------------------------- #
    def upsert(self, items: Iterable[tuple[str, list[float], dict]]) -> int:
        np = self._np
        vecs, n = [], 0
        for _id, vec, meta in items:
            v = np.asarray(vec, dtype="float32")
            if self.dim is None:
                self.dim = int(v.shape[0])
            self._ids.append(_id)
            self._meta.append(meta or {})
            vecs.append(v)
            n += 1
        if not vecs:
            return 0
        block = np.vstack(vecs)
        block /= (np.linalg.norm(block, axis=1, keepdims=True) + 1e-9)
        self._mat = block if self._mat is None else np.vstack([self._mat, block])
        if self.backend == "faiss":
            self._rebuild_faiss()
        return n

    def _rebuild_faiss(self) -> None:
        import faiss  # optional dependency; only imported when backend="faiss"
        idx = faiss.IndexFlatIP(self.dim)
        idx.add(self._mat)
        self._faiss_index = idx

    # ---- read ----------------------------------------------------------- #
    def query(self, vector: list[float], top_k: int = 5) -> list[Match]:
        np = self._np
        if self._mat is None or not self._ids:
            return []
        q = np.asarray(vector, dtype="float32")
        q /= (np.linalg.norm(q) + 1e-9)
        if self.backend == "faiss" and self._faiss_index is not None:
            scores, idxs = self._faiss_index.search(q.reshape(1, -1), min(top_k, len(self._ids)))
            pairs = zip(idxs[0].tolist(), scores[0].tolist())
            return [Match(self._ids[i], float(s), self._meta[i]) for i, s in pairs if i != -1]
        sims = self._mat @ q
        top = np.argsort(-sims)[:top_k]
        return [Match(self._ids[i], float(sims[i]), self._meta[i]) for i in top]

    def delete(self, ids: Iterable[str]) -> int:
        np = self._np
        drop = set(ids)
        keep = [i for i, _id in enumerate(self._ids) if _id not in drop]
        removed = len(self._ids) - len(keep)
        self._ids = [self._ids[i] for i in keep]
        self._meta = [self._meta[i] for i in keep]
        self._mat = self._mat[keep] if self._mat is not None and keep else None
        if self.backend == "faiss" and self._mat is not None:
            self._rebuild_faiss()
        return removed

    def __len__(self) -> int:
        return len(self._ids)


class PgVectorStore:
    """Production shape: vectors in your own Postgres via the pgvector extension.

    This keeps PHI-adjacent embeddings inside the database you already run (covered
    by your cloud BAA) instead of a separate managed service. Wire ``conn`` to your
    psycopg/SQLAlchemy connection. DDL once per corpus:

        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE dialog_chunks (id text PRIMARY KEY, embedding vector(1536), meta jsonb);
        CREATE INDEX ON dialog_chunks USING hnsw (embedding vector_cosine_ops);
    """

    def __init__(self, conn, table: str = "dialog_chunks"):
        self.conn = conn
        self.table = table

    def upsert(self, items):
        n = 0
        with self.conn.cursor() as cur:
            for _id, vec, meta in items:
                cur.execute(
                    f"INSERT INTO {self.table} (id, embedding, meta) VALUES (%s, %s, %s) "
                    f"ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, meta = EXCLUDED.meta",
                    (_id, vec, meta),
                )
                n += 1
        self.conn.commit()
        return n

    def query(self, vector, top_k: int = 5):
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT id, 1 - (embedding <=> %s) AS score, meta FROM {self.table} "
                f"ORDER BY embedding <=> %s LIMIT %s",
                (vector, vector, top_k),
            )
            return [Match(r[0], float(r[1]), r[2] or {}) for r in cur.fetchall()]

    def delete(self, ids):
        with self.conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE id = ANY(%s)", (list(ids),))
            n = cur.rowcount
        self.conn.commit()
        return n


if __name__ == "__main__":
    import numpy as np, time

    rng = np.random.default_rng(0)
    store = LocalVectorStore(dim=64)
    # a few labeled "dialogue chunks" plus noise
    store.upsert([("gi_bleed", rng.standard_normal(64).tolist(), {"topic": "rectal bleeding"})])
    target = store._mat[0].tolist()
    store.upsert([(f"noise_{i}", rng.standard_normal(64).tolist(), {"topic": "other"}) for i in range(5000)])

    t = time.time()
    hits = store.query(target, top_k=3)
    dt = (time.time() - t) * 1000
    print(f"index size: {len(store)} vectors")
    print(f"query time : {dt:.1f} ms (exact cosine, numpy)")
    print("top hit    :", hits[0].id, round(hits[0].score, 3), hits[0].metadata)
    assert hits[0].id == "gi_bleed", "nearest neighbor should be the planted vector"
    print("\nPASS — Pinecone-style upsert/query running locally over 5,001 vectors, no managed service.")
