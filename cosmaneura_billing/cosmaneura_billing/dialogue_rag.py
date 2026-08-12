"""
Dialogue retrieval-augmented generation for the "leading questions" feature
(prototype feature3), now backed by your own vector store instead of Pinecone.

End-to-end shape:
    corpus (patient-doctor dialogues)
        -> Embedder.embed(...)            # MockEmbedder offline, OpenAIEmbedder in prod
        -> VectorStore.upsert(...)        # PgVectorStore in prod, LocalVectorStore in dev
    patient utterance
        -> Embedder.embed([utterance])
        -> VectorStore.query(...)         # nearest prior dialogues
        -> LLMClient.complete_json(...)   # propose the next screening questions

Production wiring uses PgVectorStore (pgvector in your own Postgres, covered by
your cloud BAA) and a BAA-covered embedding provider. The demo below runs fully
offline (MockEmbedder + LocalVectorStore + MockLLM) so you can see it work.

Ethics note carried over from the billing design: the generated questions
complete the clinical history for the patient's benefit; they never steer the
patient toward additional billable services.

Self-test:  python -m cosmaneura_billing.dialogue_rag
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Protocol

from .llm import LLMClient, MockLLM
from .vectorstore import LocalVectorStore, Match, VectorStore


# --------------------------------------------------------------------------- #
# Embedders
# --------------------------------------------------------------------------- #
class Embedder(Protocol):
    dim: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class MockEmbedder:
    """Deterministic hashed bag-of-words embedding. Offline, no API. Similar text
    -> similar vectors, which is enough to demonstrate retrieval quality."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in re.findall(r"[a-z]+", t.lower()):
                if len(tok) < 3:
                    continue
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            out.append(v)
        return out


class OpenAIEmbedder:
    """Production embeddings via the OpenAI API. Use only on a ZDR-enabled,
    BAA-covered project for PHI. The prototype used ada-002; text-embedding-3-small
    is the current, cheaper default (1536 dims)."""

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536, api_key: str | None = None):
        self.model = model
        self.dim = dim
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/embeddings", data=body,
            headers={"content-type": "application/json", "authorization": f"Bearer {self.api_key}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
        return [d["embedding"] for d in payload["data"]]


# --------------------------------------------------------------------------- #
# RAG
# --------------------------------------------------------------------------- #
class DialogueRAG:
    def __init__(self, embedder: Embedder, store: VectorStore):
        self.embedder = embedder
        self.store = store

    def index(self, corpus: list[dict[str, Any]]) -> int:
        """corpus items: {"id": str, "text": str, **metadata}."""
        vecs = self.embedder.embed([c["text"] for c in corpus])
        items = [(c["id"], v, {k: val for k, val in c.items() if k != "id"})
                 for c, v in zip(corpus, vecs)]
        return self.store.upsert(items)

    def retrieve(self, patient_text: str, top_k: int = 4) -> list[Match]:
        q = self.embedder.embed([patient_text])[0]
        return self.store.query(q, top_k=top_k)

    @staticmethod
    def build_prompt(patient_text: str, matches: list[Match]) -> tuple[str, str]:
        ctx = "\n".join(f"- {m.metadata.get('text', '')}" for m in matches)
        system = (
            "You are a clinical intake assistant. Using similar prior encounters as "
            "reference, propose the next few screening questions that complete this "
            "patient's history for safe, accurate care. Ask about red-flag symptoms, "
            "relevant exposures, duration and severity. Never steer the patient toward "
            "tests or services for billing reasons; ask only what benefits their care."
        )
        user = (f"TASK: leading_questions\n\nPatient said:\n{patient_text}\n\n"
                f"Similar prior encounters:\n{ctx}\n\n"
                "Return JSON: {\"questions\": [\"...\", \"...\"]}")
        return system, user

    def suggest_questions(self, patient_text: str, llm: LLMClient, top_k: int = 4) -> dict:
        matches = self.retrieve(patient_text, top_k=top_k)
        system, user = self.build_prompt(patient_text, matches)
        result = llm.complete_json(system=system, user=user)
        result["_retrieved"] = [m.id for m in matches]
        return result


# small synthetic corpus for the offline demo (no PHI, no licensing)
_DEMO_CORPUS = [
    {"id": "d1", "text": "Patient reports bright red blood in stool after spicy food and constipation; worried about cancer.", "topic": "gi_bleed"},
    {"id": "d2", "text": "Patient has rectal bleeding with weight loss; advised colonoscopy and stool testing.", "topic": "gi_bleed"},
    {"id": "d3", "text": "Patient with throbbing one-sided headache, light sensitivity, nausea; migraine pattern.", "topic": "headache"},
    {"id": "d4", "text": "Patient with productive cough, fever, and shortness of breath for five days.", "topic": "respiratory"},
    {"id": "d5", "text": "Patient with burning on urination and urinary frequency; possible UTI.", "topic": "uti"},
    {"id": "d6", "text": "Patient reports black tarry stools and lightheadedness; concern for upper GI bleeding.", "topic": "gi_bleed"},
]


if __name__ == "__main__":
    rag = DialogueRAG(embedder=MockEmbedder(dim=256), store=LocalVectorStore())
    n = rag.index(_DEMO_CORPUS)
    print(f"indexed {n} dialogues into LocalVectorStore (swap for PgVectorStore in prod)\n")

    query = "When I eat spicy food I see a little blood when I go to the bathroom, and I've been constipated."
    matches = rag.retrieve(query, top_k=3)
    print("query:", query)
    print("nearest prior dialogues:")
    for m in matches:
        print(f"  {m.id}  score={m.score:.3f}  topic={m.metadata.get('topic')}")
    assert matches[0].metadata.get("topic") == "gi_bleed", "top hit should be a GI-bleed dialogue"

    out = rag.suggest_questions(query, llm=MockLLM(), top_k=3)
    print("\nleading questions (grounded in retrieved cases):")
    for q in out.get("questions", []):
        print("  •", q)

    print("\nPASS — dialogue RAG runs end to end over a local vector store; "
          "set PgVectorStore + OpenAIEmbedder for production (no Pinecone).")
