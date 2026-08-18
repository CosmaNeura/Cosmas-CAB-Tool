"""Application lifecycle, readiness, and same-origin frontend serving."""
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import cosmaneura_billing.api as api
from cosmaneura_billing.settings import Settings


def _warm_engine():
    return SimpleNamespace(
        ncci=SimpleNamespace(loaded=True),
        coverage=SimpleNamespace(loaded=True),
    )


def test_lifespan_warms_engine_before_readiness(monkeypatch, tmp_path):
    calls = []

    def warm(settings, **_kwargs):
        calls.append(settings.environment)
        return _warm_engine()

    monkeypatch.setattr(api, "get_orchestrator", warm)
    application = api.create_app(frontend_dist=tmp_path / "missing")
    client = TestClient(application)

    assert client.get("/api/ready").status_code == 503
    with client:
        assert client.get("/api/ready").json() == {
            "status": "ready",
            "ncciLoaded": True,
            "coverageLoaded": True,
        }
        assert calls == ["development"]
    assert client.get("/api/ready").status_code == 503


def test_startup_failure_never_marks_application_ready(monkeypatch, tmp_path):
    def fail(settings, **_kwargs):
        raise RuntimeError("knowledge base unavailable")

    monkeypatch.setattr(api, "get_orchestrator", fail)
    application = api.create_app(frontend_dist=tmp_path / "missing")
    with pytest.raises(RuntimeError, match="knowledge base unavailable"):
        with TestClient(application):
            pass
    assert application.state.ready is False
    assert application.state.startup_error == "RuntimeError"


def test_missing_regulatory_dataset_fails_startup_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api,
        "get_orchestrator",
        lambda settings, **_kwargs: SimpleNamespace(
            ncci=SimpleNamespace(loaded=True),
            coverage=SimpleNamespace(loaded=False),
        ),
    )
    application = api.create_app(frontend_dist=tmp_path / "missing")

    with pytest.raises(RuntimeError, match="regulatory datasets"):
        with TestClient(application):
            pass
    assert application.state.ready is False


def test_liveness_is_shallow_while_readiness_is_warming(tmp_path):
    client = TestClient(api.create_app(frontend_dist=tmp_path / "missing"))

    assert client.get("/api/live").json() == {"status": "alive"}
    assert client.get("/api/ready").status_code == 503


def test_built_frontend_is_same_origin_without_shadowing_api(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<main>CosmaNeura frontend</main>", encoding="utf-8"
    )
    (assets / "app.js").write_text(
        "window.loaded = true;", encoding="utf-8"
    )

    client = TestClient(api.create_app(frontend_dist=dist))
    assert client.get("/").text == "<main>CosmaNeura frontend</main>"
    assert client.get("/assets/app.js").text == "window.loaded = true;"
    assert client.get("/api/ready").json() == {"status": "warming"}


def test_production_refuses_to_start_without_built_frontend(tmp_path):
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
    with pytest.raises(RuntimeError, match="built same-origin frontend"):
        api.create_app(
            settings=settings, frontend_dist=tmp_path / "missing-frontend"
        )
