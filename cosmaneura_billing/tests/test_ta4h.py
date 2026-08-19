"""Azure Text Analytics for health client + secondary coding agent."""
import json

import pytest

from cosmaneura_billing.agents.ta4h_coding import TA4HCodingAgent
from cosmaneura_billing.evidence import verify_suggestion_evidence
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.schemas import CodeKind, Disposition
from cosmaneura_billing.ta4h import (
    AzureTA4H, MockTA4H, get_default_ta4h_client, parse_entities,
)
from demo_visit import SAMPLE_VISIT


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def test_parse_entities_reads_assertions_and_links():
    doc = {
        "id": "signed_note",
        "entities": [
            {"text": "hematochezia", "category": "SymptomOrSign", "offset": 10,
             "length": 12, "confidenceScore": 0.98, "name": "Hematochezia",
             "links": [{"dataSource": "UMLS", "id": "C0018932"},
                       {"dataSource": "ICD10CM", "id": "K62.5"}]},
            {"text": "colorectal cancer", "category": "Diagnosis", "offset": 50,
             "length": 17, "confidenceScore": 0.9,
             "assertion": {"certainty": "negative", "association": "other"},
             "links": [{"dataSource": "ICD10CM", "id": "C18.9"}]},
        ],
    }
    ents = parse_entities(doc, "signed_note")
    assert len(ents) == 2

    first = ents[0]
    assert first.icd10 == "K62.5"
    assert first.source == "signed_note"
    assert first.affirmed_for_patient is True

    second = ents[1]
    assert second.negated is True
    assert second.about_someone_else is True
    assert second.affirmed_for_patient is False


def test_parse_entities_drops_offshape_rows_instead_of_raising():
    doc = {"entities": [
        "not-a-dict",
        {"text": "x"},                                     # missing category/offset
        {"text": "y", "category": "Diagnosis", "offset": "0", "length": 1},
        {"text": "z", "category": "Diagnosis", "offset": 0, "length": 1},
    ]}
    ents = parse_entities(doc, "signed_note")
    assert [e.text for e in ents] == ["z"]


def test_first_link_per_vocabulary_wins():
    doc = {"entities": [{
        "text": "a", "category": "Diagnosis", "offset": 0, "length": 1,
        "links": [{"dataSource": "ICD10CM", "id": "E11.9"},
                  {"dataSource": "ICD10CM", "id": "E11.8"}],
    }]}
    assert parse_entities(doc, "s")[0].icd10 == "E11.9"


# --------------------------------------------------------------------------- #
# Request construction / PHI posture
# --------------------------------------------------------------------------- #
def test_submit_body_opts_out_of_input_logging(monkeypatch):
    """The PHI-relevant default: loggingOptOut must be sent as true."""
    client = AzureTA4H(endpoint="https://example.cognitiveservices.azure.com",
                       api_key="k")
    seen: dict = {}

    def fake_request(url, *, data=None, method="GET"):
        if method == "POST":
            seen["body"] = json.loads(data)
            return {}, {"operation-location": "https://example/jobs/1"}
        return {"status": "succeeded", "tasks": {"items": [
            {"results": {"documents": [{"id": "signed_note", "entities": []}]}}]}}, {}

    monkeypatch.setattr(client, "_request", fake_request)
    client.analyze({"signed_note": "text"})

    task = seen["body"]["tasks"][0]
    assert task["kind"] == "Healthcare"
    assert task["parameters"]["loggingOptOut"] is True


def test_analyze_skips_empty_documents_without_calling_out(monkeypatch):
    client = AzureTA4H(endpoint="https://example", api_key="k")
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: pytest.fail("should not call the service"))
    assert client.analyze({"signed_note": "", "transcript": "   "}) == {}


def test_missing_operation_location_raises(monkeypatch):
    client = AzureTA4H(endpoint="https://example", api_key="k")
    monkeypatch.setattr(client, "_request", lambda *a, **k: ({}, {}))
    with pytest.raises(RuntimeError, match="operation-location"):
        client.analyze({"signed_note": "text"})


def test_failed_job_raises(monkeypatch):
    client = AzureTA4H(endpoint="https://example", api_key="k")

    def fake_request(url, *, data=None, method="GET"):
        if method == "POST":
            return {}, {"operation-location": "https://example/jobs/1"}
        return {"status": "failed", "errors": [{"code": "InternalServerError"}]}, {}

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(RuntimeError, match="failed"):
        client.analyze({"signed_note": "text"})


def test_analyze_requires_configuration():
    with pytest.raises(RuntimeError, match="AZURE_LANGUAGE_ENDPOINT"):
        AzureTA4H(endpoint="", api_key="").analyze({"signed_note": "text"})


# --------------------------------------------------------------------------- #
# Default client selection — off unless explicitly configured
# --------------------------------------------------------------------------- #
def test_disabled_by_default(monkeypatch):
    for var in ("AZURE_LANGUAGE_ENDPOINT", "AZURE_LANGUAGE_KEY", "COSMANEURA_TA4H"):
        monkeypatch.delenv(var, raising=False)
    assert get_default_ta4h_client() is None


def test_mock_selected_by_env(monkeypatch):
    monkeypatch.setenv("COSMANEURA_TA4H", "mock")
    assert isinstance(get_default_ta4h_client(), MockTA4H)


def test_explicit_off_beats_configuration(monkeypatch):
    monkeypatch.setenv("AZURE_LANGUAGE_ENDPOINT", "https://example")
    monkeypatch.setenv("AZURE_LANGUAGE_KEY", "k")
    monkeypatch.setenv("COSMANEURA_TA4H", "off")
    assert get_default_ta4h_client() is None


def test_azure_client_selected_when_configured(monkeypatch):
    monkeypatch.delenv("COSMANEURA_TA4H", raising=False)
    monkeypatch.setenv("AZURE_LANGUAGE_ENDPOINT", "https://example")
    monkeypatch.setenv("AZURE_LANGUAGE_KEY", "k")
    assert isinstance(get_default_ta4h_client(), AzureTA4H)


# --------------------------------------------------------------------------- #
# The mock's spans are real spans
# --------------------------------------------------------------------------- #
def test_mock_offsets_point_at_real_text():
    docs = {"signed_note": SAMPLE_VISIT.signed_note,
            "intake_text": SAMPLE_VISIT.intake_text}
    for source, entities in MockTA4H().analyze(docs).items():
        for ent in entities:
            sliced = docs[source][ent.offset:ent.offset + ent.length]
            assert sliced.lower() == ent.text.lower()


def test_mock_is_silent_on_unrelated_text():
    assert MockTA4H().analyze({"signed_note": "Routine annual eye exam, no findings."}) == {}


# --------------------------------------------------------------------------- #
# The secondary coding agent
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def ta4h_result():
    icd10 = ICD10CCSR()
    return TA4HCodingAgent(MockTA4H(), icd10).run(SAMPLE_VISIT)


def test_secondary_agent_never_proposes_em(ta4h_result):
    _, suggestions = ta4h_result
    assert all(s.kind != CodeKind.EM for s in suggestions), \
        "TA4H has no MDM model and must never propose an E/M level"


def test_secondary_agent_facts_never_touch_leveling_keys(ta4h_result):
    facts, _ = ta4h_result
    for leveling_key in ("problems", "data_reviewed", "risk", "total_time_minutes"):
        assert leveling_key not in facts, \
            f"'{leveling_key}' from the secondary engine could move the E/M ceiling"
    assert all(k.startswith("ta4h_") for k in facts)


def test_secondary_agent_finds_the_rectal_hemorrhage_code(ta4h_result):
    _, suggestions = ta4h_result
    assert "K62.5" in {s.code for s in suggestions}


def test_denied_family_history_is_rejected_by_assertion(ta4h_result):
    facts, suggestions = ta4h_result
    z80 = [s for s in suggestions if s.code.startswith("Z80")]
    assert z80, "the mock documents a denied family history"
    assert z80[0].disposition == Disposition.REJECTED
    assert "Z800" in facts["ta4h_contradictions"]


def test_every_suggestion_is_tagged_to_the_secondary_engine(ta4h_result):
    _, suggestions = ta4h_result
    assert all(s.source_engine == "ta4h" for s in suggestions)


def test_rationale_distinguishes_a_umls_link_from_our_own_descriptor_match():
    """The audit trail must not claim TA4H linked a code when the code actually
    came from our keyword matcher — different strength of evidence."""
    icd10 = ICD10CCSR()

    class OneEntity:
        def __init__(self, links):
            self.links = links

        def analyze(self, documents):
            from cosmaneura_billing.ta4h import HealthEntity
            text = documents["signed_note"]
            offset = text.find("Constipation")
            return {"signed_note": [HealthEntity(
                text="Constipation", category="Diagnosis", offset=offset,
                length=len("Constipation"), source="signed_note", confidence=0.9,
                name="Constipation", links=self.links)]}

    bundle_kw = {"encounter_id": "T", "patient_age": 30, "patient_sex": "male",
                 "encounter_type": "office_established", "place_of_service": "11",
                 "signed_note": "Assessment: Constipation, dietary fiber."}
    from cosmaneura_billing.schemas import VisitBundle
    bundle = VisitBundle(**bundle_kw)

    linked = TA4HCodingAgent(OneEntity({"ICD10CM": "K59.00"}), icd10).run(bundle)[1]
    assert linked and "through its UMLS ICD-10-CM mapping" in linked[0].rationale
    assert "ta4h_resolved_by:umls_link" in linked[0].notes

    unlinked = TA4HCodingAgent(OneEntity({"UMLS": "C0009806"}), icd10).run(bundle)[1]
    assert unlinked and "returned no ICD-10-CM link" in unlinked[0].rationale
    assert "our own" in unlinked[0].rationale
    assert "ta4h_resolved_by:descriptor_match" in unlinked[0].notes


def test_offset_anchored_evidence_verifies_exactly(ta4h_result):
    """The payoff of character offsets: nothing is stripped by the verifier."""
    _, suggestions = ta4h_result
    with_evidence = [s for s in suggestions if s.evidence]
    assert with_evidence, "expected at least one evidence-bearing suggestion"
    before = {s.code: len(s.evidence) for s in with_evidence}
    audit = verify_suggestion_evidence(SAMPLE_VISIT, with_evidence)
    after = {s.code: len(s.evidence) for s in with_evidence}
    assert before == after, f"verifier dropped offset-anchored evidence: {audit}"
    assert not [line for line in audit if "EVIDENCE-REMOVED" in line]
