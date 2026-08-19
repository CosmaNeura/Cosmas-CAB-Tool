"""FastAPI integration for the closed, PHI-safe runtime log boundary."""
from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("fastapi")

from fastapi import Request
from fastapi.testclient import TestClient

import cosmaneura_billing.api as api
from cosmaneura_billing.runtime import PHISafeHTTPMiddleware, is_valid_request_id
from cosmaneura_billing.settings import Settings


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[int, str, dict[str, object]]] = []

    def log(
        self, level: int, message: str, *, extra: dict[str, object],
    ) -> None:
        self.events.append((level, message, extra))


def _application(monkeypatch, tmp_path, *, cors_origins=()):
    logger = _RecordingLogger()
    configured_levels: list[str] = []

    def configure(level: str):
        configured_levels.append(level)
        return logger

    monkeypatch.setattr(api, "configure_json_logging", configure)
    application = api.create_app(
        settings=Settings(log_level="WARNING", cors_origins=cors_origins),
        frontend_dist=tmp_path / "missing-frontend",
    )
    assert configured_levels == ["WARNING"]
    assert application.user_middleware[0].cls is PHISafeHTTPMiddleware
    return application, logger


def test_request_id_is_server_owned_available_on_state_and_exposed(
    monkeypatch, tmp_path,
):
    origin = "https://pilot.example"
    application, logger = _application(
        monkeypatch, tmp_path, cors_origins=(origin,),
    )
    observed: dict[str, str] = {}

    @application.get("/api/runtime-state/{patient_label}")
    async def runtime_state(patient_label: str, request: Request):
        del patient_label
        observed["request_id"] = request.state.request_id
        return {"status": "ok"}

    inbound_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    response = TestClient(application).get(
        "/api/runtime-state/Jane-Secret?diagnosis=Private",
        headers={"Origin": origin, "X-Request-ID": inbound_id},
    )

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert is_valid_request_id(request_id)
    assert request_id != inbound_id.replace("-", "")
    assert observed == {"request_id": request_id}
    exposed = {
        value.strip().lower()
        for value in response.headers["Access-Control-Expose-Headers"].split(",")
    }
    assert exposed == {"retry-after", "x-request-id"}

    assert len(logger.events) == 1
    level, message, event = logger.events[0]
    assert level == logging.INFO
    assert message == "http_request"
    assert event == {
        "event": "http_request",
        "request_id": request_id,
        "method": "GET",
        "route": "/api/runtime-state/{patient_label}",
        "status": 200,
        "duration_ms": event["duration_ms"],
    }
    assert isinstance(event["duration_ms"], float)


def test_unhandled_error_is_generic_and_logs_no_request_material(
    monkeypatch, tmp_path,
):
    application, logger = _application(monkeypatch, tmp_path)

    @application.post("/api/runtime-error/{patient_label}")
    async def runtime_error(patient_label: str, request: Request):
        body = await request.body()
        raise RuntimeError(
            f"unsafe diagnostic {patient_label} {body.decode('utf-8')} "
            f"{request.headers.get('X-Private-Note')}"
        )

    secrets = ["Jane-Secret", "Rare-Diagnosis", "Private-Body", "Private-Header"]
    response = TestClient(application).post(
        f"/api/runtime-error/{secrets[0]}?diagnosis={secrets[1]}",
        content=secrets[2],
        headers={"X-Private-Note": secrets[3]},
    )

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]
    assert response.json() == {
        "detail": "Internal server error",
        "requestId": request_id,
    }
    assert response.headers["Cache-Control"] == "no-store"

    assert len(logger.events) == 1
    level, message, event = logger.events[0]
    assert level == logging.ERROR
    assert message == "http_request"
    assert event["request_id"] == request_id
    assert event["route"] == "/api/runtime-error/{patient_label}"
    assert event["status"] == 500
    assert event["exception"] == "RuntimeError"

    rendered_boundary = json.dumps(
        {"response": response.json(), "log": event}, sort_keys=True,
    )
    for secret in secrets:
        assert secret not in rendered_boundary
