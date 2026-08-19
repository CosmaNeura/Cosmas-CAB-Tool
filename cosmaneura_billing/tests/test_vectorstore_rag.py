"""Vector store + dialogue RAG (feature3's Pinecone replacement). Needs numpy."""
import pytest

np = pytest.importorskip("numpy")

from cosmaneura_billing.dialogue_rag import (
    DialogueRAG,
    MockEmbedder,
    OpenAIEmbedder,
    _DEMO_CORPUS,
)
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.settings import Settings, SettingsError
from cosmaneura_billing.vectorstore import LocalVectorStore


def test_local_store_nearest_neighbor():
    rng = np.random.default_rng(0)
    store = LocalVectorStore(dim=64)
    store.upsert([("gi_bleed", rng.standard_normal(64).tolist(), {"topic": "rectal bleeding"})])
    target = store._mat[0].tolist()
    store.upsert([(f"noise_{i}", rng.standard_normal(64).tolist(), {"topic": "other"})
                  for i in range(500)])
    hits = store.query(target, top_k=3)
    assert hits[0].id == "gi_bleed"
    assert hits[0].score >= hits[1].score


def test_dialogue_rag_end_to_end():
    rag = DialogueRAG(embedder=MockEmbedder(dim=256), store=LocalVectorStore())
    n = rag.index(_DEMO_CORPUS)
    assert n == len(_DEMO_CORPUS)

    query = ("When I eat spicy food I see a little blood when I go to the bathroom, "
             "and I've been constipated.")
    matches = rag.retrieve(query, top_k=3)
    assert matches[0].metadata.get("topic") == "gi_bleed"

    out = rag.suggest_questions(query, llm=MockLLM(), top_k=3)
    assert out.get("questions")


def test_direct_openai_embeddings_are_rejected_in_deployed_environments():
    settings = Settings(
        environment="production",
        auth_mode="container_apps",
        entra_tenant_id="11111111-1111-4111-8111-111111111111",
        state_store="postgres",
        chase_store="postgres",
        database_url="postgresql://cosmaneura_app@example.invalid/db",
        database_runtime_role="cosmaneura_app",
        practice_id="practice-a",
    )
    with pytest.raises(SettingsError, match="disabled in deployed environments"):
        OpenAIEmbedder(
            api_key="synthetic-key",
            settings=settings,
        )
