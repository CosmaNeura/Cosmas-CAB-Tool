"""Synthetic-only local demo composition and process-boundary contracts."""
from __future__ import annotations

from dataclasses import replace
import errno
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing.chase.synthetic_demo import EXPECTED_PACK_DIGEST
from cosmaneura_billing.chase.worklist import Worklist
from cosmaneura_billing.local_demo import (
    LOCAL_DEMO_HOST,
    LOCAL_TEACHING_VISIT,
    _require_available_port,
    build_frontend,
    build_local_demo_app,
    build_local_demo_chase_store,
    build_local_demo_encounter_store,
    local_demo_settings,
    sanitized_child_environment,
)
from cosmaneura_billing.persistence import (
    LOCAL_SYNTHETIC_DEMO_SOURCE,
    PersistenceUnavailable,
    PostgresEncounterStore,
    SYNTHETIC_DEMO_SOURCE,
)
from cosmaneura_billing.settings import Settings


def test_demo_settings_are_explicit_local_and_transport_inert(tmp_path):
    settings = local_demo_settings(
        frontend_dist=tmp_path / "frontend",
        state_dir=tmp_path / "state",
    )

    assert LOCAL_DEMO_HOST == "127.0.0.1"
    assert settings.environment == "development"
    assert settings.auth_mode == "disabled"
    assert settings.development_roles == ("Admin",)
    assert settings.state_store == "local"
    assert settings.chase_store == "memory"
    assert settings.azure_openai_enabled is False
    assert settings.azure_language_enabled is False
    assert settings.azure_language_shadow_enabled is False
    assert settings.telemetry_enabled is False
    assert settings.cpt_auto_fetch is False
    assert settings.patient_messaging_enabled is False
    assert settings.patient_messaging_operationally_activated is False
    assert settings.chase_scheduler_enabled is False
    assert settings.chase_transport == "record_only"
    assert settings.database_url == settings.chase_database_url == ""


def test_child_and_frontend_build_environment_drop_secrets_and_vite_overrides(
    monkeypatch, tmp_path,
):
    hostile = {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "DATABASE_URL": "postgresql://secret",
        "OPENAI_API_KEY": "secret",
        "AZURE_OPENAI_API_KEY": "secret",
        "AZURE_CLIENT_SECRET": "secret",
        "AMA_CONSUMER_SECRET": "secret",
        "COSMANEURA_PATIENT_MESSAGING_ENABLED": "true",
        "HTTPS_PROXY": "https://proxy.invalid",
        "PYTHONPATH": "/hostile/import/path",
        "VITE_API_URL": "https://external.invalid",
        "VITE_SECRET": "secret",
    }
    clean = sanitized_child_environment(hostile)

    assert clean == {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "PYTHONUNBUFFERED": "1",
    }
    isolated = sanitized_child_environment(
        hostile, isolated_home=tmp_path / "isolated-home",
    )
    assert isolated["HOME"] == str(tmp_path / "isolated-home")

    # The actual build helper uses the same allowlist. Stub only the build
    # process; no install or network command is permitted by the implementation.
    monkeypatch.setenv("VITE_API_URL", "https://external.invalid")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        out = Path(command[command.index("--outDir") + 1])
        (out / "index.html").write_text("<main>synthetic</main>", encoding="utf-8")

    monkeypatch.setattr("cosmaneura_billing.local_demo.subprocess.run", fake_run)
    monkeypatch.setattr(
        "cosmaneura_billing.local_demo._frontend_tools_available",
        lambda *_args: True,
    )
    build_frontend(tmp_path / "dist")

    assert captured["command"][0] == "node"
    assert captured["command"][1].endswith("/node_modules/vite/bin/vite.js")
    assert captured["command"][2] == "build"
    assert captured["command"][3:5] == ["--configLoader", "runner"]
    assert captured["command"][5:7] == ["--mode", "local-demo"]
    assert "VITE_API_URL" not in captured["env"]
    assert "DATABASE_URL" not in captured["env"]


def test_encounter_store_is_digest_verified_read_only_and_deterministic():
    first = build_local_demo_encounter_store()
    second = build_local_demo_encounter_store()
    first_rows = {row.bundle.encounter_id: row for row in first.all()}
    second_rows = {row.bundle.encounter_id: row for row in second.all()}

    assert len(first_rows) == 13
    assert set(first_rows) == set(second_rows)
    assert {
        encounter_id: row.resource_id for encounter_id, row in first_rows.items()
    } == {
        encounter_id: row.resource_id for encounter_id, row in second_rows.items()
    }
    assert first_rows["ENC-100237"].source == LOCAL_SYNTHETIC_DEMO_SOURCE
    assert all(
        row.source == SYNTHETIC_DEMO_SOURCE
        for encounter_id, row in first_rows.items()
        if encounter_id != "ENC-100237"
    )
    with pytest.raises(PersistenceUnavailable, match="cannot be changed"):
        first.upsert(replace(
            LOCAL_TEACHING_VISIT,
            signed_note=LOCAL_TEACHING_VISIT.signed_note + " altered",
        ))
    postgres = object.__new__(PostgresEncounterStore)
    postgres.context = None
    with pytest.raises(PersistenceUnavailable, match="reserved import provenance"):
        postgres.upsert(LOCAL_TEACHING_VISIT, source=LOCAL_SYNTHETIC_DEMO_SOURCE)


def test_chase_store_starts_with_exactly_four_fresh_approved_cases():
    first = build_local_demo_chase_store()
    second = build_local_demo_chase_store()

    assert first.counts()["open"] == second.counts()["open"] == 4
    assert first.counts()["by_bucket"] == {
        "missing": 2,
        "expired": 1,
        "eligibility": 1,
    }
    first_item = first.all_items()[0]
    first_item.assignee = "changed-in-first-run"
    assert all(
        item.assignee != "changed-in-first-run"
        for item in second.all_items()
    )


def test_local_app_exposes_admin_persona_fixed_cases_and_strong_complete_review(
    tmp_path,
):
    app = build_local_demo_app(
        frontend_dist=tmp_path / "missing-frontend",
        state_dir=tmp_path / "state",
        readiness_token="test-run-token",
    )

    with TestClient(app) as client:
        ready = client.get("/api/ready")
        session = client.get("/api/session")
        encounters = client.get("/api/encounters")
        worklist = client.post("/api/chase/worklist", json={})
        review = client.post(
            "/api/encounters/coder-view",
            json={"encounterId": "ENC-100237"},
        )
        blocked_review = client.post(
            "/api/encounters/coder-view",
            json={"encounterId": "SYN-RC-0003"},
        )
        reseed = client.post(
            "/api/chase/demo-seed",
            json={"seedDigest": EXPECTED_PACK_DIGEST},
        )

    assert ready.status_code == 200
    assert ready.headers["x-cosmas-demo-run"] == "test-run-token"
    assert session.status_code == 200
    assert session.json()["persona"] == "Administrator"
    assert session.json()["views"] == ["provider", "coder", "chase"]
    assert session.json()["capabilities"] == {"manageChase": True}
    assert len(encounters.json()) == 13
    assert worklist.json()["counts"]["open"] == 4
    assert reseed.json()["replayed"] is True
    assert reseed.json()["installedCaseCount"] == 4
    assert review.status_code == 200
    assert review.json()["dataClass"] == LOCAL_SYNTHETIC_DEMO_SOURCE
    assert review.json()["readOnly"] is True
    assert review.json()["completeness"]["complete"] is True
    assert review.json()["completeness"]["rulepack"] == "telehealth.v1"
    assert review.json()["em"]["code"] == "99214"
    assert [row["code"] for row in review.json()["dx"]] == [
        "K62.5", "K59.00", "R19.7",
    ]
    assert review.json()["degraded"] is None
    assert blocked_review.status_code == 200
    assert blocked_review.json()["completeness"]["complete"] is False
    assert blocked_review.json()["degraded"]["reason"] == (
        "offline_model_unsupported_encounter"
    )


@pytest.fixture()
def boundary_client(tmp_path):
    return TestClient(build_local_demo_app(
        frontend_dist=tmp_path / "missing-frontend",
        state_dir=tmp_path / "state",
    ))


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/billing"),
    ("POST", "/api/encounters/billing"),
    ("POST", "/api/chase/detect"),
    ("POST", "/api/chase/policy/create"),
    ("POST", "/api/chase/policy/activate"),
    ("POST", "/api/chase/release-message"),
    ("POST", "/api/athena/sync"),
    ("POST", "/api/source-import"),
    ("POST", "/api/scheduler/run"),
    ("POST", "/api/messages/send"),
    ("GET", "/api/feedback/summary"),
])
def test_local_boundary_rejects_non_demo_and_arbitrary_input_routes(
    boundary_client, method, path,
):
    # A rejected request never enters the app lifespan or warms the engine.
    response = boundary_client.request(method, path, json={})

    assert response.status_code == 403
    assert response.json() == {
        "detail": "the local demo accepts only fixed synthetic UI workflows",
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].startswith(
        "default-src 'self'",
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_demo_refuses_an_already_bound_port(monkeypatch):
    closed = []

    class OccupiedSocket:
        def bind(self, _address):
            raise OSError(errno.EADDRINUSE, "address already in use")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "cosmaneura_billing.local_demo.socket.socket",
        lambda *_args: OccupiedSocket(),
    )
    with pytest.raises(RuntimeError, match="already in use"):
        _require_available_port(8765)
    assert closed == [True]


def test_deployed_app_rejects_chase_store_override(tmp_path):
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
    from cosmaneura_billing.api import create_app

    with pytest.raises(ValueError, match="cannot override"):
        create_app(
            settings=settings,
            frontend_dist=tmp_path / "missing-frontend",
            chase_store=Worklist(),
        )
