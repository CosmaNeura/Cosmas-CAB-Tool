"""Authorized worklist has bounded saved metadata without invoking inference."""
from dataclasses import replace

import pytest

from cosmaneura_billing.billing_header import BillingHeaderError, source_snapshot
from cosmaneura_billing.persistence import PersistenceUnavailable
from test_billing_header_api import client, generate, edit_intent, ENCOUNTER


def row(client):
    response = client.get("/api/encounters")
    assert response.status_code == 200
    return next(row for row in response.json() if row["id"] == ENCOUNTER)


def test_list_reloaded_saved_progress_without_inference(client, monkeypatch):
    assert row(client)["headerProgress"]["status"] == "not_generated"
    view = generate(client)
    assert client.post("/api/encounters/header/save", json=edit_intent(view)).status_code == 200
    monkeypatch.setattr(client.app.state.orchestrator, "process", lambda *a, **k: pytest.fail("list cannot invoke model"))
    saved = row(client)["headerProgress"]
    assert saved["status"] == "awaiting_review" and saved["revisionNumber"] == 1
    assert set(saved) == {"schemaVersion", "encounterVersion", "bundleSha256", "ingestionSource", "status", "revisionNumber", "reviewedRevision"}
    store = client.app.state.encounter_store
    store.upsert(replace(store.get(ENCOUNTER).bundle, signed_note="New synthetic source."))
    assert row(client)["headerProgress"]["status"] == "not_generated"


@pytest.mark.parametrize("failure", [BillingHeaderError("source_changed"), BillingHeaderError("store_invalid"), PersistenceUnavailable("unavailable")])
def test_unavailable_summary_keeps_authorized_queue_without_false_empty(client, monkeypatch, failure):
    def unavailable(*args, **kwargs): raise failure
    monkeypatch.setattr(client.app.state.billing_header_store, "progress", unavailable)
    assert row(client)["headerProgress"]["status"] == "unavailable"


def test_membership_denial_is_not_an_unavailable_or_empty_summary(client, monkeypatch):
    def denied(*args, **kwargs): raise BillingHeaderError("not_authorized")
    monkeypatch.setattr(client.app.state.billing_header_store, "progress", denied)
    assert client.get("/api/encounters").status_code == 403
    object.__setattr__(client.app.state.settings, "development_roles", ("BillingStaff",))
    assert client.get("/api/encounters").status_code == 403


def test_header_off_retains_previous_list_shape(client):
    object.__setattr__(client.app.state.settings, "billing_header_enabled", False)
    assert "headerProgress" not in row(client)
