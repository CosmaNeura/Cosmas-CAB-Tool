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
from cosmaneura_billing.security import SecurityHeadersMiddleware
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
    assert application.user_middleware[0].cls is SecurityHeadersMiddleware
    assert application.user_middleware[1].cls is PHISafeHTTPMiddleware
    return application, logger


def test_browser_security_headers_cover_success_and_error_responses(
    monkeypatch, tmp_path,
):
    application, _ = _application(monkeypatch, tmp_path)
    client = TestClient(application)

    responses = (client.get("/api/live"), client.get("/missing"))
    assert [response.status_code for response in responses] == [200, 404]
    for response in responses:
        assert response.headers["Content-Security-Policy"] == (
            "default-src 'self'; base-uri 'self'; connect-src 'self'; "
            "font-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; "
            "style-src 'self'"
        )
        assert response.headers["Permissions-Policy"] == (
            "camera=(), geolocation=(), microphone=()"
        )
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "Strict-Transport-Security" not in response.headers
    assert responses[0].headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in responses[1].headers


def test_deployed_https_headers_override_weaker_downstream_values():
    async def downstream(_scope, _receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"x-frame-options", b"SAMEORIGIN"),
                (b"strict-transport-security", b"max-age=0"),
                (b"cache-control", b"public, max-age=3600"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    app = SecurityHeadersMiddleware(downstream, deployed_https=True)
    response = TestClient(app).get("/")

    assert response.headers.get_list("X-Frame-Options") == ["DENY"]
    assert response.headers.get_list("Strict-Transport-Security") == [
        "max-age=31536000; includeSubDomains"
    ]
    # This request is not under /api/, so downstream cache policy is retained.
    assert response.headers.get_list("Cache-Control") == ["public, max-age=3600"]


def test_api_no_store_overrides_a_cacheable_success_response():
    async def downstream(_scope, _receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"cache-control", b"private, max-age=300")],
        })
        await send({"type": "http.response.body", "body": b"{}"})

    app = SecurityHeadersMiddleware(downstream, deployed_https=True)
    response = TestClient(app).get("/api/encounters")

    assert response.status_code == 200
    assert response.headers.get_list("Cache-Control") == ["no-store"]


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
