"""ICD-10/CCSR knowledge base: bundled data loads and lookups resolve."""
import pytest

from cosmaneura_billing.knowledge.icd10_ccsr import (
    ICD10CCSR,
    _format_icd10,
    embed_search,
)
from cosmaneura_billing.settings import Settings, SettingsError


def test_bundled_code_set_loads():
    kb = ICD10CCSR()
    assert kb.size > 100  # subset at minimum; full gz ships ~74k


def test_get_by_code_with_or_without_decimal():
    kb = ICD10CCSR()
    assert kb.get("K62.5") is not None
    assert kb.get("K625").code == "K62.5"


def test_find_by_description():
    kb = ICD10CCSR()
    hits = kb.find_by_description("Hemorrhage of anus and rectum", limit=1)
    assert hits and hits[0].code == "K62.5"
    assert hits[0].ccsr_category


def test_format_inserts_decimal():
    assert _format_icd10("K5900") == "K59.00"
    assert _format_icd10("I10") == "I10"


def test_legacy_semantic_search_is_rejected_in_deployed_environments():
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
        embed_search(
            "synthetic query",
            "not-read.feather",
            api_key="synthetic-key",
            settings=settings,
        )
