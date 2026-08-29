"""Safety contract for the dormant provider suggested-codes adapter."""
import dataclasses
import json
from pathlib import Path

import pytest

from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Degradation, Disposition, Evidence
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.suggested_codes import build_suggested_codes_payload

GOLDEN = Path(__file__).parents[1] / "frontend/src/operationalSuggestedCodes.golden.json"


def _build(result=None):
    result = result or BillingOrchestrator(
        llm=MockLLM(), settings=Settings(),
    ).process(SAMPLE_VISIT)
    return result, build_suggested_codes_payload(
        SAMPLE_VISIT,
        result,
        engine_version="local-demo.v1",
        generated_at="2026-08-29T14:00:00Z",
    )


def test_full_pipeline_codes_have_exact_evidence_spans():
    result, payload = _build()
    assert payload["pipeline"]["stages"] == [
        "CodingAgent", "DocumentationIntegrityAgent",
        "ComplianceGuardrailAgent", "DenialRiskAgent",
    ]
    assert payload["human_verification_required"] is True
    assert payload["enabled"] is False
    assert {"99214", "K62.5"} <= {row["code"] for row in payload["codes"]}
    assert len(payload["codes"]) == len(result.provider_card.captured_codes)

    sources = {
        "signed_note": SAMPLE_VISIT.signed_note,
        "transcript": SAMPLE_VISIT.transcript,
        "intake_text": SAMPLE_VISIT.intake_text,
    }
    for row in payload["codes"]:
        assert row["status"] == "suggested"
        assert row["copy_text"] == row["code"]
        assert row["evidence_spans"]
        for span in row["evidence_spans"]:
            loc = span["loc"]
            assert sources[span["source"]][loc["start"]:loc["end"]] == span["text"]
    assert payload["copy_all"].split() == [row["code"] for row in payload["codes"]]


def test_missing_and_invalid_evidence_fail_closed():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    em, primary, constipation, diarrhea = (
        result.biller_worksheet.em_code,
        *result.biller_worksheet.diagnoses,
    )
    em.evidence = []
    primary.evidence = [Evidence(
        "signed_note", "INTERMITTENT HEMATOCHEZIA", "normalized-only match",
    )]
    constipation.evidence = [Evidence(
        "signed_note", "Sometimes when I have constipation", "mislabeled source",
    )]
    diarrhea.evidence = None

    _, payload = _build(result)
    assert payload["codes"] == []
    assert payload["withheld_counts"]["suggested_without_exact_evidence"] == 4


def test_held_and_removed_are_reasoned_noncopyable_decisions():
    result, payload = _build()
    reviews = {row["code"]: row for row in payload["review_items"]}
    assert reviews["E66.8"]["status"] == "held"
    assert reviews["E66.8"]["action"] == "provider_review_required"
    assert reviews["Z80.0"]["status"] == "removed"
    assert reviews["Z80.0"]["action"] == "do_not_use"
    assert all(row["status_reason"] and row["copy_text"] is None for row in reviews.values())
    assert not ({"E66.8", "Z80.0"} & set(payload["copy_all"].split()))

    result.provider_card.declined_codes.append(CodeSuggestion(
        kind=CodeKind.ICD10,
        code="Z99.89",
        label="Synthetic unexplained review item",
        disposition=Disposition.REVIEW,
        evidence=[Evidence("signed_note", "intermittent hematochezia", "test anchor")],
        rationale="",
    ))
    _, guarded = _build(result)
    assert "Z99.89" not in {row["code"] for row in guarded["review_items"]}
    assert guarded["withheld_counts"]["review_without_reason"] == 1


def test_cross_surface_conflicts_fail_closed():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    conflicted = result.biller_worksheet.diagnoses[0]
    result.provider_card.declined_codes.append(dataclasses.replace(
        conflicted, disposition=Disposition.REJECTED,
        rationale="Synthetic conflicting removal.",
    ))
    result.biller_worksheet.diagnoses[1] = dataclasses.replace(
        result.biller_worksheet.diagnoses[1], rationale="Worksheet-only mutation",
    )

    _, payload = _build(result)
    visible = {row["code"] for row in payload["codes"]}
    assert conflicted.code not in visible
    assert result.biller_worksheet.diagnoses[1].code not in visible
    assert payload["withheld_counts"]["cross_surface_conflict"] == 2


def test_degraded_result_hides_every_code_decision():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    expected = len(result.provider_card.captured_codes + result.provider_card.declined_codes)
    result.degraded = Degradation("test_degradation", "Synthetic degraded result.")
    _, payload = _build(result)
    assert payload["status"] == "degraded"
    assert payload["codes"] == payload["review_items"] == []
    assert payload["copy_all"] == ""
    assert payload["withheld_counts"]["degraded_result"] == expected


def test_adapter_is_read_only_and_rejects_cross_encounter_results():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    before = dataclasses.asdict(SAMPLE_VISIT), dataclasses.asdict(result)
    _build(result)
    assert before == (dataclasses.asdict(SAMPLE_VISIT), dataclasses.asdict(result))

    result.provider_card.encounter_id = "another-encounter"
    with pytest.raises(ValueError, match="same encounter"):
        _build(result)


def test_full_pipeline_golden_fixture():
    _, payload = _build()
    expected = {
        "documents": {
            "signed_note": SAMPLE_VISIT.signed_note,
            "transcript": SAMPLE_VISIT.transcript,
            "intake_text": SAMPLE_VISIT.intake_text,
        },
        "payload": payload,
    }
    rendered = json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    assert GOLDEN.read_text(encoding="utf-8") == rendered
    assert payload["generated_at"] == "2026-08-29T14:00:00Z"
    assert payload["engine_version"] == "local-demo.v1"
