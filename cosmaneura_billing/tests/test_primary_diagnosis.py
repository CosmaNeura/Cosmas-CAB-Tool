"""Primary intent survives holds; synthetic replay, never model/network calls."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.agents.coding import CodingAgent
from cosmaneura_billing.agents.denial_risk import DenialRiskAgent
from cosmaneura_billing.api import _billing_json, _coder_view_json
from cosmaneura_billing.billing_header_runtime import generated_header
from cosmaneura_billing.billing_header_store import MemoryBillingHeaderStore
from cosmaneura_billing.billing_tab import build_billing_tab_view
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.primary_diagnosis import INTENT, intent_notes, resolve_primary, result_primary
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.suggested_codes import build_suggested_codes_payload

QUOTE = "Cough is assessed. Hypertension is controlled; continue current treatment."


@pytest.fixture(scope="module")
def catalogue():
    return ICD10CCSR()


def candidate(code="I10", *, primary=False, disposition=Disposition.CAPTURED, quote=QUOTE):
    return CodeSuggestion(CodeKind.ICD10, code, "Synthetic diagnosis",
        disposition=disposition, evidence=[Evidence("signed_note", quote)] if quote else [],
        notes=[INTENT] if primary else [], necessity_supported=True,
        rationale="Exact synthetic documentation retained for review.")


class RecordedLLM:
    mode = "fixture"

    def __init__(self, diagnoses):
        self.outputs = iter([{}, diagnoses])

    def complete_json(self, **_kwargs):
        return deepcopy(next(self.outputs))


def pipeline(items, catalogue):
    bundle = VisitBundle("SYN-PRIMARY", 40, "F", "office_established", "11", signed_note=QUOTE)

    class Coder:
        def run(self, _bundle):
            return {}, deepcopy(items)

    result = BillingOrchestrator(coding=Coder(), llm=RecordedLLM({}), icd10=catalogue,
        settings=Settings(preferences_enabled=False)).process(bundle)
    return bundle, result


@pytest.mark.parametrize("value,expected", [(True, [INTENT]), (False, []),
    ("false", ["primary_intent_invalid"]), ("true", ["primary_intent_invalid"]),
    (1, ["primary_intent_invalid"]), (0, ["primary_intent_invalid"]),
    (None, ["primary_intent_invalid"])])
def test_intent_is_strict_boolean_and_malformed_values_do_not_fall_back(value, expected):
    notes = intent_notes({"primary": value})
    assert notes == expected and intent_notes({}) == []
    item = candidate(); item.notes = notes
    if "primary_intent_invalid" in notes:
        assert resolve_primary([item]).primary is None


def test_unresolved_extraction_retains_intent_and_exact_quote(catalogue):
    quote = "Recent pregnancy loss; completion and location remain unconfirmed."
    bundle = VisitBundle("SYN-INTENT", 31, "F", "office_established", "11", signed_note=quote)
    _, suggestions = CodingAgent(RecordedLLM({"diagnoses": [{"concept": quote,
        "primary": True, "quote": quote}]}), catalogue).run(bundle)
    dx = next(s for s in suggestions if s.kind == CodeKind.ICD10)
    assert dx.code == "(unresolved)" and INTENT in dx.notes
    assert dx.evidence[0].quote == quote and dx.rationale


@pytest.mark.parametrize("disposition", [Disposition.REVIEW, Disposition.REJECTED])
@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_nonfinal_intent_blocks_substitute_without_mutation(disposition, reverse):
    first, second = candidate("(unresolved)", primary=True, disposition=disposition), candidate()
    items = [first, second][::-1] if reverse else [first, second]
    before = deepcopy(items)
    resolution = resolve_primary(items)
    assert resolution.status == "unresolved_explicit" and resolution.primary is None
    assert items == before and resolution.intents == (first,)


def test_no_evidence_primary_and_conflicting_duplicates_do_not_select_secondary():
    first, second = candidate("R05.8", primary=True, quote=""), candidate()
    assert resolve_primary([first, second]).primary is None
    first = candidate(primary=True)
    assert resolve_primary([first, deepcopy(first)]).status == "resolved_explicit"
    conflict = deepcopy(first); conflict.disposition = Disposition.REVIEW
    assert resolve_primary([first, conflict]).status == "conflicting_explicit"
    assert resolve_primary([first, candidate("R05.8", primary=True)]).primary is None


def test_stale_captured_flag_does_not_override_negative_necessity():
    first = candidate("R05.8", primary=True)
    first.necessity_supported = False
    resolution = resolve_primary([first, candidate()])
    assert resolution.primary is None and resolution.status == "unresolved_explicit"


def test_absent_intent_keeps_labeled_legacy_order_and_valid_primary_wins():
    first, second = candidate(), candidate("R05.8")
    fallback = resolve_primary([first, second])
    assert fallback.primary is first and fallback.status == "legacy_fallback"
    assert fallback.public["requires_review"] and "provisional" in fallback.public["message"]
    second.notes = [INTENT]
    assert resolve_primary([first, second]).primary is second


@pytest.mark.parametrize("kind", ["invalid_current", "rejected", "unresolved", "conflict"])
def test_denial_gate_never_uses_secondary_for_unresolved_primary_coverage(catalogue, kind):
    first = candidate("R05" if kind == "invalid_current" else "(unresolved)", primary=True)
    if kind == "rejected":
        first.disposition = Disposition.REJECTED
    second = candidate(primary=kind == "conflict")
    procedure = CodeSuggestion(CodeKind.PROCEDURE, "12345", "Synthetic service",
        disposition=Disposition.CAPTURED, evidence=[Evidence("signed_note", QUOTE)])

    class Coverage:
        loaded = True

        def supports(self, *_args):
            pytest.fail("substitute diagnosis reached primary-based coverage")

    _, flags = DenialRiskAgent().run([first, second, procedure], icd10=catalogue, coverage=Coverage())
    assert second.disposition == Disposition.CAPTURED
    assert any("not evaluated" in flag for flag in flags)
    assert not any("not_supported" in note or "covered for" in note for note in procedure.notes)


@pytest.mark.parametrize("primary", [True, False])
def test_resolved_and_legacy_coverage_paths_remain_compatible(catalogue, primary):
    calls = []

    class Coverage:
        loaded = True

        def supports(self, service, diagnosis):
            from types import SimpleNamespace
            calls.append((service, diagnosis))
            return SimpleNamespace(status="no_policy")

    procedure = CodeSuggestion(CodeKind.PROCEDURE, "12345", "Synthetic service",
        disposition=Disposition.CAPTURED, evidence=[Evidence("signed_note", QUOTE)])
    DenialRiskAgent().run([candidate(primary=primary), procedure], icd10=catalogue, coverage=Coverage())
    assert calls == [("12345", "I10")]


def test_resolve_after_preference_consensus_current_code_or_rejection_hold(catalogue):
    for note, code, disposition in [
        ("preference_hold: confirm", "R05.8", Disposition.CAPTURED),
        ("consensus_hold: primary only", "R05.8", Disposition.CAPTURED),
        ("", "R05", Disposition.CAPTURED),
        ("", "R05.8", Disposition.REJECTED),
    ]:
        first = candidate(code, primary=True, disposition=disposition)
        if note:
            first.notes.append(note)
        _, result = pipeline([first, candidate()], catalogue)
        assert result.biller_worksheet.primary_diagnosis is None
        assert [d.code for d in result.biller_worksheet.diagnoses] == ["I10"]
        assert result.biller_worksheet.requires_provider_signoff


def test_api_views_keep_secondaries_but_do_not_badge_raw_intent(catalogue):
    bundle, result = pipeline([candidate("(unresolved)", primary=True), candidate()], catalogue)
    # Stale metadata/pointer must not override the final object states.
    result.biller_worksheet.primary_diagnosis = result.biller_worksheet.diagnoses[0]
    raw = _billing_json(bundle, result)
    coder = _coder_view_json(bundle, result, "synthetic-coder")
    tab = build_billing_tab_view(bundle, result, engine_version="test")
    suggested = build_suggested_codes_payload(bundle, result, engine_version="test")
    assert raw["primary"] is None and not any(s.get("primary") for s in raw["captured"] + raw["declined"])
    assert not any(s["primary"] for s in coder["dx"] + coder["withheld"])
    assert not any(s["primary"] for s in tab["icd_candidates"] + suggested["codes"])
    assert [s["code"] for s in suggested["codes"]] == ["I10"]
    assert tab["copy_all"] == suggested["copy_all"] == "I10"
    assert all(s["copy_text"] is None and not s["selectable"] for s in suggested["review_items"])
    assert raw["diagnosisSequencing"] == coder["diagnosisSequencing"] == tab["diagnosis_sequencing"]


@pytest.mark.parametrize("has_quote", [True, False])
def test_primary_review_header_flag_is_exact_and_immutable_on_save(catalogue, has_quote):
    bundle, result = pipeline([candidate("(unresolved)", primary=True, quote=QUOTE if has_quote else ""), candidate()], catalogue)
    encounters = MemoryEncounterStore([bundle], practice_id="primary-test")
    stored = encounters.get(bundle.encounter_id)
    generated = generated_header(stored, result, engine_version="test")
    flag = next(f for f in generated["flags"] if f["id"] == "primary_diagnosis_sequencing")
    assert "unresolved" in flag["message"]
    assert bool(flag.get("evidenceIds")) == has_quote
    for eid in flag.get("evidenceIds", []):
        ev = next(e for e in generated["evidence"] if e["id"] == eid)
        assert ev["text"] == bundle.signed_note[ev["start"]:ev["end"]] == QUOTE
    assert [s["code"] for s in generated["suggestions"]] == ["I10"]
    h.validate_generated(generated, stored)
    store = MemoryBillingHeaderStore(encounters, practice_id="primary-test", authorize=lambda _: True)
    actor = h.HeaderActor(str(uuid4()), str(uuid4()), "Synthetic Reviewer", "Coder")
    view, _ = store.create(bundle.encounter_id, expected_source=h.source_snapshot(stored),
        generated=generated, actor=actor, operation_id=str(uuid4()))
    draft = h.create_draft(view["header"], None); draft["note"] = "Review retained secondary diagnosis."
    saved, _ = store.save(bundle.encounter_id, header_id=view["header"]["headerId"],
        expected_source=h.source_snapshot(stored), expected_revision=0, draft=draft,
        reason="Synthetic review", actor=actor, operation_id=str(uuid4()))
    reloaded = store.latest(bundle.encounter_id, expected_source=h.source_snapshot(stored))
    assert reloaded["header"]["flags"] == saved["header"]["flags"] == view["header"]["flags"]
    assert encounters.get(bundle.encounter_id).bundle == bundle
