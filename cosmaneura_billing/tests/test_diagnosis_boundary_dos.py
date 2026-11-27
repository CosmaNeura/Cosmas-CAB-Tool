"""Diagnosis sentence boundaries must stay linear in note length.

``_boundaries`` lowercases and regex-scans a fresh copy of the entire prefix at
every semicolon, newline, or sentence break. These tests fail while that rescan
is in place. They pass once each boundary looks at a bounded window and the
same work is not repeated for the whole note.

The sizes below are far under the 250,000-character intake limit on
``POST /api/billing``. A linear scan of the longer note finishes well under the
budget; the prefix rescan takes several seconds.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cosmaneura_billing.agents.coding import CodingAgent
from cosmaneura_billing.agents.ta4h_coding import TA4HCodingAgent
from cosmaneura_billing.api import create_app
from cosmaneura_billing.knowledge.diagnosis_terms import (
    _boundaries,
    anchor_context,
    supported_by_quote,
)
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.schemas import VisitBundle
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.ta4h import HealthEntity

# Verbatim demo sentence. The offline mock treats it as the encounter signature
# and returns it as a diagnosis quote.
SIGNATURE = "When I eat spicy food, I poop blood"

# Four times as many separators. A prefix rescan grows about sixteen times;
# a bounded scan grows about four times. Eight times leaves room for noise
# without accepting the quadratic curve.
_GROWTH = 4
_MAX_GROWTH = 8
_SMALL_SEPARATORS = 4_000
_LARGE_SEPARATORS = _SMALL_SEPARATORS * _GROWTH

_TENANT_ID = "11111111-1111-4111-8111-111111111111"
_PROVIDER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_CODER_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
_ADMIN_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
_STAFF_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _note(separators: int) -> str:
    return SIGNATURE + ("a;" * separators)


def _bundle(separators: int) -> VisitBundle:
    return VisitBundle(
        encounter_id="ADHOC-1",
        patient_age=26,
        patient_sex="male",
        encounter_type="office_established",
        place_of_service="11",
        intake_text=_note(separators),
        transcript="",
        signed_note="",
    )


def _elapsed(fn):
    started = time.perf_counter()
    value = fn()
    return time.perf_counter() - started, value


def _assert_bounded(label: str, small_s: float, large_s: float, budget_s: float) -> None:
    """Fail the prefix-rescan curve, and accept a bounded scan on a slow host."""
    within_budget = large_s < budget_s
    within_growth = large_s < small_s * _MAX_GROWTH
    assert within_budget or within_growth, (
        f"{label} took {large_s:.3f}s after {_LARGE_SEPARATORS} separators "
        f"({small_s:.3f}s at {_SMALL_SEPARATORS}; growth {large_s / small_s:.1f}x "
        f"for {_GROWTH}x input). A bounded scan stays under {budget_s:.2f}s "
        f"or grows less than {_MAX_GROWTH}x."
    )


def test_abbreviation_periods_stay_inside_the_sentence():
    text = "Seen by Dr. Smith today. Follow up."
    assert _boundaries(text) == [0, text.index("Follow"), len(text)]
    context = anchor_context("Smith", text)
    assert context is not None
    assert "Dr. Smith" in context
    assert "Follow" not in context


def test_boundaries_stay_linear_in_the_number_of_separators():
    _boundaries(_note(100))
    small_s, small_bounds = _elapsed(lambda: _boundaries(_note(_SMALL_SEPARATORS)))
    large_s, large_bounds = _elapsed(lambda: _boundaries(_note(_LARGE_SEPARATORS)))

    assert len(small_bounds) == _SMALL_SEPARATORS + 2
    assert len(large_bounds) == _LARGE_SEPARATORS + 2
    _assert_bounded("_boundaries", small_s, large_s, budget_s=0.20)


def test_anchor_context_stays_linear_for_one_unique_quote():
    _elapsed(lambda: anchor_context(SIGNATURE, _note(100)))
    small_s, small_context = _elapsed(
        lambda: anchor_context(SIGNATURE, _note(_SMALL_SEPARATORS))
    )
    large_s, large_context = _elapsed(
        lambda: anchor_context(SIGNATURE, _note(_LARGE_SEPARATORS))
    )

    assert small_context is not None and large_context is not None
    assert SIGNATURE in small_context
    assert SIGNATURE in large_context
    assert len(large_context) < len(SIGNATURE) + 8
    _assert_bounded("anchor_context", small_s, large_s, budget_s=0.20)


def test_supported_by_quote_stays_linear_for_many_semicolons():
    description = "Hemorrhage of anus and rectum"
    small_n = 2_000
    large_n = small_n * _GROWTH

    def quote(separators: int) -> str:
        return "a;" * separators

    supported_by_quote(description, quote(100))
    small_s, small_ok = _elapsed(lambda: supported_by_quote(description, quote(small_n)))
    large_s, large_ok = _elapsed(lambda: supported_by_quote(description, quote(large_n)))

    assert small_ok is False and large_ok is False
    within_budget = large_s < 0.50
    within_growth = large_s < small_s * _MAX_GROWTH
    assert within_budget or within_growth, (
        f"supported_by_quote took {large_s:.3f}s for {large_n} semicolons "
        f"({small_s:.3f}s at {small_n}; growth {large_s / small_s:.1f}x). "
        "A bounded scan stays under 0.50s or grows less than "
        f"{_MAX_GROWTH}x."
    )


def test_mock_coding_agent_anchors_a_padded_intake_without_a_prefix_rescan():
    agent = CodingAgent(MockLLM(), ICD10CCSR())
    agent.run(_bundle(0))

    def suggestions(separators: int):
        return agent.run(_bundle(separators))[1]

    small_s, small_suggestions = _elapsed(lambda: suggestions(_SMALL_SEPARATORS))
    large_s, large_suggestions = _elapsed(lambda: suggestions(_LARGE_SEPARATORS))

    def anchored(suggestions) -> bool:
        return any(
            evidence.quote == SIGNATURE
            for suggestion in suggestions
            for evidence in suggestion.evidence
        )

    assert anchored(small_suggestions)
    assert anchored(large_suggestions)
    _assert_bounded("CodingAgent.run", small_s, large_s, budget_s=0.40)


def test_ta4h_coding_agent_anchors_a_padded_intake_without_a_prefix_rescan():
    class OneDiagnosis:
        mode = "test"

        def analyze(self, documents):
            text = documents["intake_text"]
            offset = text.find(SIGNATURE)
            return {
                "intake_text": [
                    HealthEntity(
                        text=SIGNATURE,
                        category="Diagnosis",
                        offset=offset,
                        length=len(SIGNATURE),
                        source="intake_text",
                        confidence=0.99,
                        name="Hemorrhage of anus and rectum",
                        links={"ICD10CM": "K62.5"},
                    )
                ]
            }

    agent = TA4HCodingAgent(OneDiagnosis(), ICD10CCSR())
    agent.run(_bundle(0))
    small_s, _ = _elapsed(lambda: agent.run(_bundle(_SMALL_SEPARATORS)))
    large_s, _ = _elapsed(lambda: agent.run(_bundle(_LARGE_SEPARATORS)))
    _assert_bounded("TA4HCodingAgent.run", small_s, large_s, budget_s=0.40)


def _principal_header(user_id: str, role: str) -> dict[str, str]:
    payload = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "oid", "val": user_id},
            {"typ": "tid", "val": _TENANT_ID},
            {"typ": "roles", "val": role},
        ],
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"X-MS-CLIENT-PRINCIPAL": encoded}


def _app(tmp_path, *, auth_mode: str) -> TestClient:
    settings = Settings(
        environment="test",
        auth_mode=auth_mode,
        entra_tenant_id=_TENANT_ID,
        practice_id="practice-test",
        feedback_path=str(tmp_path / "feedback.jsonl"),
        access_audit_path=str(tmp_path / "audit.jsonl"),
    )
    return TestClient(
        create_app(settings=settings, frontend_dist=tmp_path / "missing-frontend"),
    )


def _quotes(body: dict) -> list[str]:
    rows = body.get("captured", []) + body.get("declined", [])
    return [
        evidence["quote"]
        for row in rows
        for evidence in row.get("evidence", [])
    ]


def _bill(client: TestClient, separators: int, headers: dict[str, str] | None = None):
    started = time.perf_counter()
    response = client.post(
        "/api/billing",
        headers=headers or {},
        timeout=30,
        json={
            "encounterId": "ADHOC-1",
            "intake": _note(separators),
            "transcript": "",
            "signedNote": "",
        },
    )
    return time.perf_counter() - started, response


def test_signed_in_provider_billing_request_stays_bounded(tmp_path):
    with _app(tmp_path, auth_mode="container_apps") as client:
        headers = _principal_header(_PROVIDER_ID, "Provider")
        warmup_s, warmup = _bill(client, 0, headers)
        assert warmup.status_code == 200, warmup.text
        assert warmup.json()["inferenceMode"] == "mock"
        assert SIGNATURE in _quotes(warmup.json())

        small_s, small = _bill(client, _SMALL_SEPARATORS, headers)
        large_s, large = _bill(client, _LARGE_SEPARATORS, headers)

    assert small.status_code == 200, small.text
    assert large.status_code == 200, large.text
    assert SIGNATURE in _quotes(small.json())
    assert SIGNATURE in _quotes(large.json())
    assert large.json()["inferenceMode"] == "mock"
    assert warmup_s < 30
    _assert_bounded("POST /api/billing", small_s, large_s, budget_s=0.40)


@pytest.mark.parametrize("role,user_id", [("Coder", _CODER_ID), ("Admin", _ADMIN_ID)])
def test_coder_and_admin_reach_the_same_billing_path(tmp_path, role, user_id):
    with _app(tmp_path, auth_mode="container_apps") as client:
        elapsed, response = _bill(client, 0, _principal_header(user_id, role))
    assert response.status_code == 200, response.text
    assert response.json()["inferenceMode"] == "mock"
    assert SIGNATURE in _quotes(response.json())
    assert elapsed < 30


def test_billing_staff_cannot_run_ad_hoc_billing(tmp_path):
    with _app(tmp_path, auth_mode="container_apps") as client:
        elapsed, response = _bill(
            client, _LARGE_SEPARATORS, _principal_header(_STAFF_ID, "BillingStaff"),
        )
    assert response.status_code == 403
    assert elapsed < 1


def test_default_offline_mode_uses_the_mock_for_the_demo_sentence(tmp_path):
    with _app(tmp_path, auth_mode="disabled") as client:
        elapsed, response = _bill(client, 0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inferenceMode"] == "mock"
    assert SIGNATURE in _quotes(body)
    assert elapsed < 30
