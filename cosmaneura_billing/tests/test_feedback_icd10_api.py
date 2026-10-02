"""Current-code enforcement applies to new reviews, not exact saved retries."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cosmaneura_billing import icd10_validity
from test_api import (
    SMITH_ID, TENANT_ID, _accepted_feedback_payload, _principal_header, client,
)


INVALID_DETAIL = "final ICD-10 code is not verified as currently billable"


@pytest.fixture(autouse=True)
def authenticated_review_settings(monkeypatch):
    # Reuse the existing client fixture with real principal parsing enabled.
    monkeypatch.setenv("COSMANEURA_AUTH_MODE", "container_apps")
    monkeypatch.setenv("COSMANEURA_ENTRA_TENANT_ID", TENANT_ID)
    monkeypatch.setenv("COSMANEURA_PRACTICE_ID", "synthetic-icd10-api")


def issued_review(client):
    response = client.post(
        "/api/encounters/coder-view", json={"encounterId": "ENC-100237"},
        headers=_principal_header(SMITH_ID, "Coder"),
    )
    assert response.status_code == 200
    view = response.json()
    assert view["coderId"] == SMITH_ID
    assert view["dx"], "The review must exercise at least one final diagnosis."
    return _accepted_feedback_payload(view, review_id=str(uuid4()))


def submit(client, payload):
    return client.post(
        "/api/feedback", json=payload,
        headers=_principal_header(SMITH_ID, "Coder"),
    )


@pytest.mark.parametrize("code", ["Z86.010", "E66.8", "Q99.9999"])
def test_invalid_or_nonbillable_human_addition_returns_422_without_write(client, code):
    payload = issued_review(client)
    payload["decisions"].append({
        "kind": "ICD10", "engine_code": None, "action": "add",
        "final_code": code, "reason_code": "dx_missed_by_engine",
        "note": "Rationale: Synthetic review proposal.\nEvidence: Synthetic note excerpt.",
    })

    response = submit(client, payload)

    assert response.status_code == 422
    assert response.json() == {"detail": INVALID_DETAIL}
    assert client.app.state.feedback_store.count() == 0
    assert not client.app.state.feedback_store.path.exists()


@pytest.mark.parametrize("catalogue_state", ["missing_current", "not_billable", "unknown"])
def test_saved_authenticated_replay_survives_catalogue_change_but_new_write_does_not(
    client, monkeypatch, catalogue_state,
):
    payload = issued_review(client)
    first = submit(client, payload)
    assert first.status_code == 200
    assert first.json() == {"ok": True, "reviewId": payload["review_id"], "replayed": False}
    store = client.app.state.feedback_store
    before = store.path.read_bytes()
    active = False if catalogue_state == "not_billable" else None
    catalogue = SimpleNamespace(
        has_current_fy=catalogue_state != "missing_current",
        is_active=lambda _code: active,
    )
    monkeypatch.setattr(icd10_validity, "_current_catalogue", lambda: catalogue)

    replay = submit(client, payload)
    assert replay.status_code == 200
    assert replay.json() == {"ok": True, "reviewId": payload["review_id"], "replayed": True}

    fresh = deepcopy(payload)
    fresh["review_id"] = str(uuid4())
    rejected = submit(client, fresh)
    assert rejected.status_code == 422
    assert rejected.json() == {"detail": INVALID_DETAIL}

    changed = deepcopy(payload)
    changed["overall_note"] = "Changed content under the already persisted review identifier."
    conflict = submit(client, changed)
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "review ID conflicts with an existing review"}
    assert store.path.read_bytes() == before
    assert store.count() == 1
