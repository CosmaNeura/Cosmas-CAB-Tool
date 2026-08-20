"""Azure Text Analytics for health client + secondary coding agent."""
import json
import time
import urllib.error

import pytest

from cosmaneura_billing.agents.ta4h_coding import TA4HCodingAgent
from cosmaneura_billing.evidence import verify_suggestion_evidence
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.schemas import CodeKind, Disposition
from cosmaneura_billing.settings import Settings, SettingsError
from cosmaneura_billing.ta4h import (
    AzureLanguageTA4H, MockTA4H, TA4HServiceError, describe_ta4h_client,
    get_default_ta4h_client, parse_entities,
)
from demo_visit import SAMPLE_VISIT

_ENDPOINT = "https://cosmaneura-language.cognitiveservices.azure.com"
_CLIENT_ID = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"


def _enabled_settings(**over) -> Settings:
    base = dict(azure_language_enabled=True, azure_language_endpoint=_ENDPOINT,
                azure_client_id=_CLIENT_ID)
    base.update(over)
    return Settings(**base)


def _client(**over) -> AzureLanguageTA4H:
    """A client with a stub token provider, so no cloud identity is needed."""
    return AzureLanguageTA4H(_enabled_settings(**over),
                             token_provider=lambda: ("stub-token", 2**40))


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def test_parse_entities_reads_assertions_and_links():
    source_text = "0123456789hematochezia" + " " * 28 + "colorectal cancer"
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
    ents = parse_entities(doc, "signed_note", source_text)
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
    ents = parse_entities(doc, "signed_note", "z")
    assert [e.text for e in ents] == ["z"]


def test_first_link_per_vocabulary_wins():
    doc = {"entities": [{
        "text": "a", "category": "Diagnosis", "offset": 0, "length": 1,
        "links": [{"dataSource": "ICD10CM", "id": "E11.9"},
                  {"dataSource": "ICD10CM", "id": "E11.8"}],
    }]}
    assert parse_entities(doc, "s", "a")[0].icd10 == "E11.9"


@pytest.mark.parametrize("offset,length", [(-1, 8), (0, 0), (100, 8)])
def test_parse_entities_rejects_invalid_bounds(offset, length):
    doc = {"entities": [{
        "text": "diabetes", "category": "Diagnosis", "offset": offset,
        "length": length, "links": [{"dataSource": "ICD10CM", "id": "E11.9"}],
    }]}
    assert parse_entities(doc, "signed_note", "Assessment: hypertension") == []


def test_parse_entities_rejects_a_concept_that_does_not_match_its_source_span():
    source = "Assessment: hypertension"
    offset = source.index("hypertension")
    doc = {"entities": [{
        "text": "diabetes", "category": "Diagnosis", "offset": offset,
        "length": len("hypertension"),
        "links": [{"dataSource": "ICD10CM", "id": "E11.9"}],
    }]}
    assert parse_entities(doc, "signed_note", source) == []


def test_unicode_code_point_offsets_match_python_source_indices():
    source = "Patient " + chr(0x1F642) + " has diabètes"
    text = "diabètes"
    offset = source.index(text)
    doc = {"entities": [{
        "text": text, "category": "Diagnosis", "offset": offset,
        "length": len(text), "confidenceScore": 0.8,
    }]}
    assert parse_entities(doc, "signed_note", source)[0].text == text


# --------------------------------------------------------------------------- #
# Request construction / PHI posture
# --------------------------------------------------------------------------- #
def test_submit_body_opts_out_of_input_logging(monkeypatch):
    """The PHI-relevant guarantee: loggingOptOut is always sent as true."""
    client = _client()
    seen: dict = {}

    def fake_request(url, *, data=None, method="GET", deadline=None):
        if method == "POST":
            seen["body"] = json.loads(data)
            seen["url"] = url
            return {}, {"operation-location": (
                _ENDPOINT + "/language/analyze-text/jobs/1?api-version=2023-04-01"
            )}
        return {"status": "succeeded", "tasks": {"items": [
            {"results": {"documents": [{"id": "signed_note", "entities": []}]}}]}}, {}

    monkeypatch.setattr(client, "_request", fake_request)
    client.analyze({"signed_note": "text"})

    task = seen["body"]["tasks"][0]
    assert task["kind"] == "Healthcare"
    assert task["parameters"]["loggingOptOut"] is True
    assert task["parameters"]["stringIndexType"] == "UnicodeCodePoint"
    assert seen["url"].startswith(_ENDPOINT + "/language/analyze-text/jobs")


def test_requests_carry_a_managed_identity_bearer_token(monkeypatch):
    client = _client()
    captured: dict = {}

    class FakeResponse:
        headers = {}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(client, "_urlopen", fake_urlopen)
    client._request(
        _ENDPOINT + "/language/analyze-text/jobs?api-version=2023-04-01",
        data=b"{}", method="POST",
    )
    assert captured["auth"] == "Bearer stub-token"


def test_redirect_response_is_terminal_and_never_followed():
    calls = {"count": 0}

    def refusing_urlopen(req, timeout=None):
        calls["count"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 302, "Jane Doe MRN-123",
            {"Location": "https://attacker.example"}, None,
        )

    client = AzureLanguageTA4H(
        _enabled_settings(), token_provider=lambda: ("stub-token", 2**40),
        urlopen=refusing_urlopen,
    )
    with pytest.raises(TA4HServiceError, match="request was rejected") as caught:
        client._request(
            _ENDPOINT + "/language/analyze-text/jobs?api-version=2023-04-01",
            data=b"{}", method="POST",
        )
    assert calls["count"] == 1
    assert "Jane" not in str(caught.value)


def test_request_timeout_is_bounded_by_the_poll_deadline():
    seen = {}

    def timing_out(req, timeout=None):
        seen["timeout"] = timeout
        raise TimeoutError("synthetic timeout")

    client = AzureLanguageTA4H(
        _enabled_settings(), token_provider=lambda: ("stub-token", 2**40),
        urlopen=timing_out, max_retries=0, timeout=60,
    )
    with pytest.raises(TA4HServiceError):
        client._request(
            _ENDPOINT + "/language/analyze-text/jobs/1",
            deadline=time.monotonic() + 0.05,
        )
    assert 0 < seen["timeout"] <= 0.05


def test_cross_host_operation_location_is_rejected_before_token_lookup(monkeypatch):
    token_calls = {"count": 0}

    def token_provider():
        token_calls["count"] += 1
        return "should-not-leave", 2**40

    client = AzureLanguageTA4H(_enabled_settings(), token_provider=token_provider)

    def fake_request(url, *, data=None, method="GET", deadline=None):
        assert method == "POST"
        return {}, {"operation-location": "https://attacker.example/steal"}

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(TA4HServiceError, match="untrusted service URL"):
        client.analyze({"signed_note": "synthetic text"})
    # The injected request seam bypasses the normal submit token on purpose;
    # validation must still reject the returned URL before any poll bearer.
    assert token_calls["count"] == 0


def test_bearer_token_is_cached_until_it_nears_expiry():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return f"token-{calls['n']}", 2**40

    client = AzureLanguageTA4H(_enabled_settings(), token_provider=provider)
    assert client._bearer() == "token-1"
    assert client._bearer() == "token-1"
    assert calls["n"] == 1


def test_expired_token_is_refreshed():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        # already expired, so every call must re-fetch
        return f"token-{calls['n']}", 0.0

    client = AzureLanguageTA4H(_enabled_settings(), token_provider=provider)
    assert client._bearer() == "token-1"
    assert client._bearer() == "token-2"


def test_api_key_in_the_environment_is_refused(monkeypatch):
    """Managed identity only: a key is a misconfiguration, not a fallback."""
    monkeypatch.setenv("AZURE_LANGUAGE_KEY", "should-not-be-used")
    with pytest.raises(SettingsError, match="prohibited"):
        AzureLanguageTA4H._build_token_provider(_CLIENT_ID)


def test_injected_token_provider_cannot_bypass_prohibited_credentials(monkeypatch):
    monkeypatch.setenv("AZURE_LANGUAGE_KEY", "should-not-be-used")
    with pytest.raises(SettingsError, match="prohibited"):
        _client()


def test_settings_reject_a_key_when_the_engine_is_enabled():
    with pytest.raises(SettingsError, match="prohibited"):
        Settings.from_env({
            "COSMANEURA_AZURE_LANGUAGE_ENABLED": "1",
            "AZURE_LANGUAGE_ENDPOINT": _ENDPOINT,
            "AZURE_CLIENT_ID": _CLIENT_ID,
            "AZURE_LANGUAGE_KEY": "nope",
        })


def test_settings_reject_a_non_azure_endpoint():
    with pytest.raises(SettingsError, match="AZURE_LANGUAGE_ENDPOINT"):
        _enabled_settings(azure_language_endpoint="https://evil.example.com")


def test_settings_require_an_endpoint_when_enabled():
    with pytest.raises(SettingsError, match="AZURE_LANGUAGE_ENDPOINT"):
        Settings(azure_language_enabled=True, azure_client_id=_CLIENT_ID)


def test_settings_require_the_managed_identity_when_enabled():
    with pytest.raises(SettingsError, match="AZURE_CLIENT_ID"):
        Settings(azure_language_enabled=True, azure_language_endpoint=_ENDPOINT)


@pytest.mark.parametrize("client_id", [
    "not-a-uuid", "{6f9619ff-8b86-d011-b42d-00cf4fc964ff}",
])
def test_settings_require_a_canonical_managed_identity_uuid(client_id):
    with pytest.raises(SettingsError, match="AZURE_CLIENT_ID"):
        _enabled_settings(azure_client_id=client_id)


def test_mock_is_refused_in_deployed_environments():
    with pytest.raises(SettingsError, match="development-only"):
        Settings(azure_language_mock=True, environment="production")


def test_mock_and_live_are_mutually_exclusive():
    with pytest.raises(SettingsError, match="cannot be combined"):
        _enabled_settings(azure_language_mock=True)


def test_client_refuses_a_non_azure_endpoint_before_sending_a_token(monkeypatch):
    """A caller must not be able to point the engine at another host and have a
    managed-identity bearer token sent to it. The client re-validates its own
    effective values, so this fails at construction, before any request."""
    monkeypatch.delenv("AZURE_LANGUAGE_KEY", raising=False)
    hostile = Settings.__new__(Settings)
    object.__setattr__(hostile, "azure_language_endpoint", "https://attacker.example.com")
    object.__setattr__(hostile, "azure_language_api_version", "2023-04-01")
    object.__setattr__(hostile, "azure_client_id", _CLIENT_ID)
    with pytest.raises(SettingsError, match="AZURE_LANGUAGE_ENDPOINT"):
        AzureLanguageTA4H(hostile)


def test_analyze_skips_empty_documents_without_calling_out(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: pytest.fail("should not call the service"))
    assert client.analyze({"signed_note": "", "transcript": "   "}) == {}


def test_missing_operation_location_raises(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *a, **k: ({}, {}))
    with pytest.raises(RuntimeError, match="operation-location"):
        client.analyze({"signed_note": "text"})


def test_failed_job_raises(monkeypatch):
    client = _client()

    def fake_request(url, *, data=None, method="GET", deadline=None):
        if method == "POST":
            return {}, {"operation-location": (
                _ENDPOINT + "/language/analyze-text/jobs/1?api-version=2023-04-01"
            )}
        return {"status": "failed", "errors": [{"message": "Jane Doe MRN-123"}]}, {}

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(TA4HServiceError, match="job failed") as caught:
        client.analyze({"signed_note": "text"})
    assert "Jane" not in str(caught.value)


@pytest.mark.parametrize("error_location", ["task", "document"])
def test_partial_service_errors_fail_closed_without_raw_provider_text(error_location):
    canary = "Jane Doe MRN-123"
    if error_location == "task":
        payload = {"status": "succeeded", "tasks": {"items": [{
            "error": {"message": canary},
        }]}}
    else:
        payload = {"status": "succeeded", "tasks": {"items": [{
            "results": {"documents": [], "errors": [{"message": canary}]},
        }]}}
    with pytest.raises(TA4HServiceError) as caught:
        AzureLanguageTA4H._collect(payload, {"signed_note": "synthetic"})
    assert canary not in str(caught.value)


def test_malformed_task_container_fails_with_a_sanitized_error():
    with pytest.raises(TA4HServiceError, match="malformed response"):
        AzureLanguageTA4H._collect(
            {"status": "succeeded", "tasks": ["bad"]},
            {"signed_note": "synthetic"},
        )


def test_success_response_must_return_every_submitted_document():
    payload = {"status": "succeeded", "tasks": {"items": [{
        "results": {"documents": [], "errors": []},
    }]}}
    with pytest.raises(TA4HServiceError, match="incomplete document results"):
        AzureLanguageTA4H._collect(
            payload, {"signed_note": "synthetic patient text"},
        )


@pytest.mark.parametrize("returned_id", ["unknown", "signed_note"])
def test_success_response_rejects_unknown_or_duplicate_document_ids(returned_id):
    documents = [{"id": "signed_note", "entities": []}]
    if returned_id == "unknown":
        documents = [{"id": "unknown", "entities": []}]
    else:
        documents.append({"id": "signed_note", "entities": []})
    payload = {"status": "succeeded", "tasks": {"items": [{
        "results": {"documents": documents, "errors": []},
    }]}}
    with pytest.raises(TA4HServiceError, match="mismatched document results"):
        AzureLanguageTA4H._collect(
            payload, {"signed_note": "synthetic patient text"},
        )


# --------------------------------------------------------------------------- #
# Default client selection — off unless explicitly enabled
# --------------------------------------------------------------------------- #
def test_disabled_by_default():
    assert get_default_ta4h_client(Settings()) is None


def test_mock_selected_by_settings():
    assert isinstance(get_default_ta4h_client(Settings(azure_language_mock=True)),
                      MockTA4H)


def test_describe_reports_the_engine_count():
    assert describe_ta4h_client(None) == {
        "extractionEngines": 1, "secondaryExtractionMode": None}
    assert describe_ta4h_client(MockTA4H()) == {
        "extractionEngines": 2, "secondaryExtractionMode": "mock"}


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
