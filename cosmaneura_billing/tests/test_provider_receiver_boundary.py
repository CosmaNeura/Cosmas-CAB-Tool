"""Receiver integration preserves optional context and separate model authority."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from cosmaneura_billing.billing_header import digest
from cosmaneura_billing.persistence import PostgresEncounterStore
from cosmaneura_billing.provider_handoff import HandoffRequest
from cosmaneura_billing.source_visit_context import read_source_visit_context
from provider_admission_support import register_route
from test_completed_note_client import setup, synthetic_envelope
from test_provider_handoff import envelope, prepared
from test_provider_handoff_postgres import context, accept


@pytest.mark.parametrize("value", [None, "", True, "2026-02-29", "0000-01-01", "2026-9-01", "09/30/2026", "2026-09-30T12:00:00Z"])
def test_provider_date_requires_an_exact_calendar_day(value):
    raw = envelope(); raw["visit"]["serviceDate"] = value
    with pytest.raises(ValidationError):
        HandoffRequest.model_validate(raw)


def test_omitted_date_preserves_original_request_and_manifest_identity():
    raw = envelope(); p = prepared(raw)
    expected = {**raw, "supportingDocuments": []}
    assert p.intent == expected and "serviceDate" not in p.manifest["visit"]
    manifest = deepcopy(expected)
    del manifest["operationId"], manifest["expectedSource"], manifest["note"]["text"]
    manifest["versionKey"] = p.manifest["versionKey"]
    assert p.manifest_sha256 == digest(manifest)


def test_restricted_pg_optional_date_replay_source_version_and_wrapper(context):
    ctx, actor = context; raw = envelope(); raw["visit"]["serviceDate"] = "2024-02-29"
    first = accept(ctx, actor, raw); store = PostgresEncounterStore(ctx)
    old = store.get(first["receipt"]["encounterId"])
    value = read_source_visit_context(old, encounter_store=store)
    assert value["serviceDate"] == "2024-02-29" and value["statusMeaning"] == "provider_attested"
    assert accept(ctx, actor, raw) == {**first, "replayed": True}
    newer = deepcopy(raw)
    newer.update(operationId=str(uuid4()), sourceRevision=2, expectedSource=first["receipt"]["headerSource"])
    del newer["visit"]["serviceDate"]
    accept(ctx, actor, newer); current = store.get(old.bundle.encounter_id)
    assert old.bundle == current.bundle and current.version == old.version + 1
    assert read_source_visit_context(old, encounter_store=store)["origin"] == "unavailable"
    assert read_source_visit_context(current, encounter_store=store)["serviceDate"] is None
    assert not accept(ctx, actor, raw)["current"]
    with ctx.connection() as db:
        assert not db.execute("SELECT has_function_privilege(current_user,'cosmaneura_commit_provider_handoff_019(jsonb)','EXECUTE') AS allowed").fetchone()["allowed"]
        assert db.execute("SELECT to_regprocedure('public.cosmaneura_provider_model_source(uuid,uuid,text,text)') AS fn").fetchone()["fn"] is not None


@pytest.mark.parametrize("flag", ["azure_openai_enabled", "azure_language_enabled", "azure_language_shadow_enabled"])
def test_storage_admission_never_admits_a_model_service(setup, flag):
    client, store, extraction, config = setup
    raw = synthetic_envelope(); register_route(client, raw)
    receipt = client.post("/api/provider/encounters/handoff", json=raw).json()["receipt"]
    # Change only server policy; no real service is constructed or contacted.
    flags = dict(azure_openai_enabled=True, azure_openai_endpoint="https://synthetic-only.openai.azure.com",
        azure_openai_deployment="synthetic-only", azure_openai_model="gpt-4.1", azure_client_id=str(uuid4()),
        azure_language_endpoint="https://synthetic-only.cognitiveservices.azure.com",
        azure_openai_synthetic_only=flag == "azure_language_shadow_enabled")
    flags[flag] = True
    client.app.state.settings = replace(config, **flags)
    detail = client.post("/api/encounters/detail", json={"encounterId": receipt["encounterId"]})
    assert detail.json()["inferencePolicy"]["allowed"] is False
    result = client.post("/api/encounters/header/generate", json={"encounterId": receipt["encounterId"],
        "expectedSource": receipt["headerSource"], "operationId": str(uuid4())})
    assert result.status_code == 403 and result.json()["detail"] == "operational_source_model_not_approved"
    assert extraction.calls == 0 and store.get(receipt["encounterId"]).bundle.signed_note == raw["note"]["text"]
