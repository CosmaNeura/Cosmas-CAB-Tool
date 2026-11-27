"""Procedure add-on detection must finish on long whitespace runs.

``RULE_99058`` matches "worked", "squeezed", or "fit" with a whitespace
quantifier, an optional word group, and a second whitespace quantifier before
"in". A run of spaces or tabs with no following "in", and no newline or
sentence punctuation to split the sentence, makes those two quantifiers try
every split. These tests fail while that pattern is in use. They pass once a
whitespace run in an allowed note is searched in linear time, or refused
before the search, on every caller below.

Not executed here, and covered only through ``BillingOrchestrator.process``:
the billing-header generate route and the header-generation worker. Both call
``process`` on the stored bundle when that release control is on. The control
defaults off and the worker needs Postgres.
"""
from __future__ import annotations

import base64
import json
import time
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cosmaneura_billing.api import create_app
from cosmaneura_billing.knowledge.procedures import ProcedureRules
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import VisitBundle
from cosmaneura_billing.settings import Settings

# 20_000 whitespace characters is far under the 250_000 / 500_000 field caps
# and the 1 MiB body cap. The overlapping quantifiers take about two seconds
# on this pattern; a single-quantifier search of the same text is under a
# millisecond. 0.15s leaves room for the rest of an in-process pipeline and
# still rejects the quadratic search.
_RUN = 20_000
_LIMIT_SECONDS = 0.15

_TENANT = "11111111-1111-4111-8111-111111111111"
_PROVIDER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_CODER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_ADMIN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _bomb(verb: str, gap: str) -> str:
    """One sentence: a trigger word, a long whitespace run, and no 'in'."""
    return f"See {verb}{gap * _RUN}x"


def _bundle(**fields: str) -> VisitBundle:
    return VisitBundle(
        encounter_id="REDOS-UNIT",
        patient_age=44,
        patient_sex="female",
        encounter_type="office_established",
        place_of_service="11",
        intake_text=fields.get("intake_text", ""),
        transcript=fields.get("transcript", ""),
        signed_note=fields.get("signed_note", ""),
    )


def _assert_fast(call, label: str) -> None:
    started = time.perf_counter()
    caught: Exception | None = None
    try:
        call()
    except Exception as exc:  # a fast refusal is an acceptable bound
        caught = exc
    elapsed = time.perf_counter() - started
    detail = f"{label} took {elapsed:.3f}s"
    if caught is not None:
        detail += f" and raised {type(caught).__name__}"
    assert elapsed < _LIMIT_SECONDS, detail


@pytest.mark.parametrize(
    ("source", "verb", "gap"),
    [
        ("signed_note", "worked", " "),
        ("signed_note", "squeezed", " "),
        ("signed_note", "fit", " "),
        ("signed_note", "WORKED", " "),
        ("signed_note", "worked", "\t"),
        ("transcript", "worked", " "),
        ("intake_text", "worked", " "),
    ],
)
def test_procedure_rules_finish_on_a_whitespace_run(source, verb, gap):
    text = _bomb(verb, gap)
    bundle = _bundle(**{source: text})
    rules = ProcedureRules()
    _assert_fast(lambda: rules.propose(bundle), f"propose {source} {verb!r}")


def test_orchestrator_finishes_on_a_whitespace_run():
    """The offline mock returns no facts for this note, then procedure rules run."""
    bundle = _bundle(transcript=_bomb("worked", " "))
    engine = BillingOrchestrator()
    _assert_fast(lambda: engine.process(bundle), "orchestrator.process")


def _principal(user_id: str, role: str) -> dict[str, str]:
    payload = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "oid", "val": user_id},
            {"typ": "tid", "val": _TENANT},
            {"typ": "roles", "val": role},
        ],
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"X-MS-CLIENT-PRINCIPAL": encoded}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    directory = tmp_path_factory.mktemp("redos")
    settings = Settings(
        environment="test",
        auth_mode="container_apps",
        entra_tenant_id=_TENANT,
        practice_id="practice-redos",
        feedback_path=str(directory / "feedback.jsonl"),
        # The product default is 10. One request is enough to stall the
        # process; the higher cap only lets this module exercise each route.
        inference_requests_per_minute=1000,
        azure_openai_enabled=False,
        azure_openai_synthetic_only=False,
    )
    app = create_app(settings=settings, frontend_dist=directory / "missing-frontend")
    with TestClient(app) as http:
        warmup = http.post(
            "/api/billing",
            headers=_principal(_PROVIDER, "Provider"),
            json={
                "encounterId": "WARM-1",
                "signedNote": "Routine follow-up. Patient doing well.",
            },
        )
        assert warmup.status_code == 200, warmup.text
        yield http


def _assert_authorized_and_fast(response, elapsed: float, label: str) -> None:
    assert response.status_code not in {401, 403, 429}, (
        f"{label} was refused with {response.status_code}: {response.text[:200]}"
    )
    assert elapsed < _LIMIT_SECONDS, (
        f"{label} returned {response.status_code} in {elapsed:.3f}s"
    )


@pytest.mark.parametrize(
    ("role", "user_id", "field"),
    [
        ("Provider", _PROVIDER, "transcript"),
        ("Coder", _CODER, "signedNote"),
        ("Admin", _ADMIN, "intake"),
    ],
)
def test_adhoc_billing_finishes_on_a_whitespace_run(client, role, user_id, field):
    body = {
        "encounterId": f"ADHOC-{role}",
        "intake": "",
        "transcript": "",
        "signedNote": "",
        field: _bomb("worked", " "),
    }
    started = time.perf_counter()
    response = client.post(
        "/api/billing",
        headers=_principal(user_id, role),
        json=body,
    )
    _assert_authorized_and_fast(
        response, time.perf_counter() - started, f"POST /api/billing {role} {field}"
    )


def _store(client, *, note: str, transcript: str = "", intake: str = "") -> str:
    encounter_id = f"REDOS-{uuid4().hex[:8]}"
    client.app.state.encounter_store.upsert(
        VisitBundle(
            encounter_id=encounter_id,
            patient_age=44,
            patient_sex="female",
            encounter_type="office_established",
            place_of_service="11",
            signed_note=note,
            transcript=transcript,
            intake_text=intake,
        )
    )
    return encounter_id


@pytest.mark.parametrize(
    ("role", "user_id", "path"),
    [
        ("Provider", _PROVIDER, "/api/encounters/billing"),
        ("Provider", _PROVIDER, "/api/encounters/billing-tab"),
        ("Coder", _CODER, "/api/encounters/coder-view"),
    ],
)
def test_stored_encounter_routes_finish_on_a_whitespace_run(client, role, user_id, path):
    encounter_id = _store(client, note=_bomb("squeezed", " "))
    started = time.perf_counter()
    response = client.post(
        path,
        headers=_principal(user_id, role),
        json={"encounterId": encounter_id},
    )
    _assert_authorized_and_fast(
        response, time.perf_counter() - started, f"POST {path} {role}"
    )
