"""PHI-safe request logging, correlation IDs, and generic failures."""
from __future__ import annotations

import io
import json
import logging
import warnings

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import cosmaneura_billing.runtime as runtime
from cosmaneura_billing.runtime import (
    CHASE_WORKER_LOG_EVENT,
    CHASE_WORKER_LOGGER_NAME,
    ChaseWorkerRunJSONFormatter,
    HTTP_LOG_EVENT,
    LLMMetricJSONFormatter,
    PHISafeHTTPMiddleware,
    PHISafeJSONFormatter,
    PHISafeServiceFormatter,
    SERVICE_LOG_EVENT,
    UNMATCHED_ROUTE,
    configure_json_logging,
    generic_error_payload,
    is_safe_chase_worker_event,
    is_safe_http_event,
    is_safe_llm_event,
    is_valid_request_id,
    json_logging_config,
    resolve_request_id,
)


REQUEST_UUID = "11111111-1111-4111-8111-111111111111"
REQUEST_HEX = "11111111111141118111111111111111"


def _capturing_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PHISafeJSONFormatter())
    logger = logging.Logger("test.phi_safe_runtime", level=logging.INFO)
    logger.addHandler(handler)
    return logger, stream


def _client() -> tuple[TestClient, io.StringIO]:
    logger, stream = _capturing_logger()
    app = FastAPI()

    @app.get("/patients/{patient_id}")
    def patient(patient_id: str):
        return {"patient": patient_id, "diagnosis": "secret response"}

    @app.get("/fail/{patient_id}")
    def fail(patient_id: str):
        raise RuntimeError(f"secret exception for {patient_id}")

    app.add_middleware(PHISafeHTTPMiddleware, logger=logger)
    return TestClient(app), stream


def _one_event(stream: io.StringIO) -> dict[str, object]:
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_request_id_accepts_only_canonical_opaque_uuid4():
    assert resolve_request_id(REQUEST_UUID) == REQUEST_HEX
    assert resolve_request_id(REQUEST_HEX) == REQUEST_HEX
    assert is_valid_request_id(REQUEST_UUID)
    assert is_valid_request_id(REQUEST_HEX)

    for unsafe in (
        None,
        "patient-Jane-12345678",
        "contains space",
        "line\nbreak",
        "11111111-1111-1111-8111-111111111111",  # UUID1
        "11111111-1111-4111-7111-111111111111",  # non-RFC variant
        b"bytes-id",
    ):
        generated = resolve_request_id(unsafe)
        assert generated != unsafe
        assert is_valid_request_id(generated)


def test_http_formatter_is_closed_and_validates_every_allowed_value():
    logger, stream = _capturing_logger()
    try:
        raise ValueError("secret SDK exception for Jane Patient")
    except ValueError:
        logger.error(
            "secret log message for Jane Patient",
            exc_info=True,
            extra={
                "event": "secret event text",
                "request_id": "patient-Jane-12345678",
                "method": "POST /patients/Jane",
                "route": "/patients/Jane-Patient?token=secret",
                "status": "500 with secret detail",
                "duration_ms": float("inf"),
                "exception": "ValueError: secret SDK exception",
                "headers": {"authorization": "Bearer secret-token"},
                "body": "secret clinical note",
                "raw_path": "/patients/Jane-Patient",
                "recipient": "jane@example.invalid",
                "prompt": "private prompt",
                "evidence": "private evidence",
            },
        )

    event = _one_event(stream)
    assert set(event) == {
        "event", "request_id", "method", "route", "status", "duration_ms",
        "exception",
    }
    assert event["event"] == HTTP_LOG_EVENT
    assert is_valid_request_id(event["request_id"])
    assert event["method"] == "UNKNOWN"
    assert event["route"] == UNMATCHED_ROUTE
    assert event["status"] == 500
    assert event["duration_ms"] == 0.0
    assert event["exception"] == "ValueError"
    assert is_safe_http_event(event)
    rendered = stream.getvalue()
    for prohibited in (
        "Jane", "secret", "authorization", "clinical note", "example.invalid",
        "private prompt", "private evidence", "/patients/Jane",
    ):
        assert prohibited not in rendered


def test_service_formatter_never_serializes_sdk_message_or_arbitrary_logger_name():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PHISafeServiceFormatter())
    logger = logging.Logger("azure.patient.Jane-Patient", level=logging.ERROR)
    logger.addHandler(handler)

    try:
        raise RuntimeError("recipient=jane@example.invalid token=synthetic-secret")
    except RuntimeError:
        logger.exception(
            "SDK failed body=private-note headers=Authorization",
            extra={"request_id": REQUEST_HEX, "patient_id": REQUEST_HEX},
        )

    event = _one_event(stream)
    assert event == {
        "event": SERVICE_LOG_EVENT,
        "logger": "azure",
        "level": "ERROR",
        "exception": "RuntimeError",
    }
    rendered = stream.getvalue()
    for prohibited in (
        "Jane", "jane@", "synthetic-secret", "private-note", "Authorization",
        REQUEST_HEX,
    ):
        assert prohibited not in rendered


def test_llm_formatter_preserves_only_validated_cost_metrics():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(LLMMetricJSONFormatter())
    logger = logging.Logger("cosmaneura_billing.llm", level=logging.INFO)
    logger.addHandler(handler)
    logger.info(
        "prompt=Jane Patient DOB 2000-01-02 token=synthetic-secret",
        extra={
            "event": "llm_request",
            "provider": "azure_openai",
            "model": "gpt-4.1-mini-2025-04-14",
            "status": "success",
            "attempt": 2,
            "latency_ms": 125,
            "input_tokens": 21,
            "output_tokens": 3,
            "cache_creation_input_tokens": 8,
            "cache_read_input_tokens": 13,
            "prompt": "Jane Patient clinical note",
            "response": "private diagnosis",
            "headers": {"x-api-key": "synthetic-secret"},
            "request_id": REQUEST_HEX,
        },
    )

    event = _one_event(stream)
    assert event == {
        "event": "llm_request",
        "provider": "azure_openai",
        "model": "gpt-4.1-mini-2025-04-14",
        "status": "success",
        "attempt": 2,
        "latency_ms": 125,
        "input_tokens": 21,
        "output_tokens": 3,
        "cache_creation_input_tokens": 8,
        "cache_read_input_tokens": 13,
    }
    assert is_safe_llm_event(event)
    rendered = stream.getvalue()
    for prohibited in (
        "Jane", "2000-01-02", "synthetic-secret", "clinical note",
        "private diagnosis", REQUEST_HEX,
    ):
        assert prohibited not in rendered


def test_llm_formatter_redacts_untrusted_labels_and_invalid_counts():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(LLMMetricJSONFormatter())
    logger = logging.Logger("cosmaneura_billing.llm", level=logging.INFO)
    logger.addHandler(handler)
    logger.info(
        "llm_request",
        extra={
            "provider": "Patient-Jane",
            "model": "gpt-jane-patient",
            "status": "Jane Patient failed",
            "attempt": "MRN-123",
            "latency_ms": "DOB-2000-01-02",
            "input_tokens": -1,
            "output_tokens": True,
            "cache_creation_input_tokens": 1_000_000_001,
            "cache_read_input_tokens": "secret",
        },
    )

    event = _one_event(stream)
    assert event == {
        "event": "llm_request",
        "provider": "unknown",
        "model": "invalid_model_label",
        "status": "unexpected_error",
        "attempt": 1,
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }
    assert is_safe_llm_event(event)
    assert "Jane" not in stream.getvalue()
    assert "2000-01-02" not in stream.getvalue()


def test_llm_formatter_rejects_free_form_model_suffix():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(LLMMetricJSONFormatter())
    logger = logging.Logger("cosmaneura_billing.llm", level=logging.INFO)
    logger.addHandler(handler)
    logger.info(
        "llm_request",
        extra={
            "provider": "azure_openai",
            "model": "gpt-4.1-mini-jane-patient",
            "status": "success",
            "attempt": 1,
            "latency_ms": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )

    event = _one_event(stream)
    assert event["model"] == "invalid_model_label"
    assert "jane" not in stream.getvalue()


def test_chase_worker_formatter_emits_only_terminal_status_and_counts():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ChaseWorkerRunJSONFormatter())
    logger = logging.Logger(CHASE_WORKER_LOGGER_NAME, level=logging.INFO)
    logger.addHandler(handler)
    logger.info(
        "patient Jane delivery=70000000-0000-4000-8000-000000000007",
        extra={
            "status": "completed",
            "quarantined": 1,
            "ambiguous": 0,
            "scheduled": 4,
            "reserved": 3,
            "claimed": 2,
            "accepted": 2,
            "permanent_failures": 0,
            "delivery_id": "70000000-0000-4000-8000-000000000007",
        },
    )

    event = _one_event(stream)
    assert event == {
        "event": CHASE_WORKER_LOG_EVENT,
        "status": "completed",
        "quarantined": 1,
        "ambiguous": 0,
        "scheduled": 4,
        "reserved": 3,
        "claimed": 2,
        "accepted": 2,
        "permanent_failures": 0,
    }
    assert is_safe_chase_worker_event(event)
    assert "Jane" not in stream.getvalue()
    assert "70000000" not in stream.getvalue()


def test_success_logs_route_template_and_returns_server_owned_request_id():
    client, stream = _client()
    response = client.get(
        "/patients/Jane%20Secret?diagnosis=private",
        headers={"X-Request-ID": REQUEST_UUID},
    )

    assert response.status_code == 200
    generated = response.headers["X-Request-ID"]
    assert is_valid_request_id(generated)
    assert generated != REQUEST_HEX
    event = _one_event(stream)
    assert event == {
        "event": HTTP_LOG_EVENT,
        "request_id": generated,
        "method": "GET",
        "route": "/patients/{patient_id}",
        "status": 200,
        "duration_ms": event["duration_ms"],
    }
    assert isinstance(event["duration_ms"], (int, float))
    assert is_safe_http_event(event)
    rendered = stream.getvalue()
    for prohibited in (
        "Jane", "diagnosis", "private", "secret response", "?",
    ):
        assert prohibited not in rendered


def test_all_inbound_request_ids_are_ignored_including_valid_uuid4():
    client, stream = _client()
    response = client.get(
        "/patients/one",
        headers=[
            ("X-Request-ID", "patient-one-12345678"),
            ("X-Request-ID", REQUEST_UUID),
        ],
    )

    resolved = response.headers["X-Request-ID"]
    assert is_valid_request_id(resolved)
    assert resolved != REQUEST_HEX
    assert _one_event(stream)["request_id"] == resolved
    assert "patient-one" not in stream.getvalue()


def test_unhandled_exception_returns_generic_500_and_logs_class_only():
    client, stream = _client()
    response = client.get(
        "/fail/Jane-Secret", headers={"X-Request-ID": REQUEST_UUID},
    )

    assert response.status_code == 500
    generated = response.headers["X-Request-ID"]
    assert is_valid_request_id(generated)
    assert generated != REQUEST_HEX
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == generic_error_payload(generated)
    event = _one_event(stream)
    assert event["route"] == "/fail/{patient_id}"
    assert event["status"] == 500
    assert event["exception"] == "RuntimeError"
    assert is_safe_http_event(event)
    rendered = stream.getvalue()
    assert "Jane" not in rendered and "secret exception" not in rendered


def test_unmatched_route_uses_constant_instead_of_raw_path():
    client, stream = _client()
    response = client.get("/private-patient-name?note=secret")

    assert response.status_code == 404
    event = _one_event(stream)
    assert event["route"] == UNMATCHED_ROUTE
    assert event["status"] == 404
    assert "private-patient-name" not in stream.getvalue()
    assert "note" not in stream.getvalue()


def test_caller_controlled_nonstandard_method_is_never_logged_verbatim():
    client, stream = _client()

    response = client.request("JANE", "/patients/secret")

    assert response.status_code == 405
    event = _one_event(stream)
    assert event["method"] == "UNKNOWN"
    assert "JANE" not in stream.getvalue()


def test_logging_config_closes_framework_and_sdk_sinks():
    config = json_logging_config("WARNING")
    assert config["disable_existing_loggers"] is True
    assert config["root"] == {
        "handlers": ["phi_safe_service"], "level": "WARNING",
    }
    assert config["loggers"]["cosmaneura_billing"] == {
        "handlers": ["phi_safe_service"],
        "level": "WARNING",
        "propagate": False,
    }
    assert config["loggers"]["cosmaneura_billing.http"]["level"] == "INFO"
    assert config["loggers"]["cosmaneura_billing.llm"] == {
        "handlers": ["phi_safe_llm"],
        "level": "INFO",
        "propagate": False,
    }
    assert config["loggers"][CHASE_WORKER_LOGGER_NAME] == {
        "handlers": ["phi_safe_chase_worker"],
        "level": "INFO",
        "propagate": False,
    }
    assert config["loggers"]["uvicorn.access"] == {
        "handlers": ["discard"], "level": "CRITICAL", "propagate": False,
    }
    assert config["loggers"]["py.warnings"] == {
        "handlers": ["phi_safe_service"],
        "level": "WARNING",
        "propagate": False,
    }
    for name in ("uvicorn", "uvicorn.error", "azure", "httpx", "httpcore"):
        assert config["loggers"][name]["propagate"] is False
        assert config["loggers"][name]["handlers"] == ["phi_safe_service"]


def test_preexisting_child_handlers_are_removed_before_logs_can_bypass_boundary():
    name = "azure.core.pipeline.patient-Jane"
    child = logging.getLogger(name)
    unsafe_stream = io.StringIO()
    child.addHandler(logging.StreamHandler(unsafe_stream))
    child.addFilter(lambda _record: True)
    child.propagate = False
    child.disabled = True
    try:
        runtime._sanitize_preexisting_child_loggers()
        assert child.handlers == []
        assert child.filters == []
        assert child.propagate is True
        assert child.disabled is False
    finally:
        child.handlers.clear()
        child.filters.clear()
        logging.root.manager.loggerDict.pop(name, None)


def test_preconfigured_uvicorn_parent_cannot_leak_from_later_child(capsys):
    parent = logging.getLogger("uvicorn")
    raw_stream = io.StringIO()
    parent.addHandler(logging.StreamHandler(raw_stream))
    parent.propagate = False

    configure_json_logging("INFO")
    logging.getLogger("uvicorn.new_patient_logger").error(
        "patient Jane MRN=synthetic-123",
    )

    captured = capsys.readouterr()
    assert raw_stream.getvalue() == ""
    assert "Jane" not in captured.err
    assert "synthetic-123" not in captured.err
    assert "Jane" not in captured.out
    assert "synthetic-123" not in captured.out
    event = json.loads(captured.out)
    assert event == {
        "event": SERVICE_LOG_EVENT,
        "logger": "uvicorn",
        "level": "ERROR",
    }


def test_uvicorn_access_critical_cannot_fall_through_last_resort(capsys):
    configure_json_logging("INFO")

    logging.getLogger("uvicorn.access").critical(
        "GET /patients/Jane?token=synthetic-secret",
    )

    captured = capsys.readouterr()
    assert "Jane" not in captured.err
    assert "synthetic-secret" not in captured.err
    assert "Jane" not in captured.out
    assert "synthetic-secret" not in captured.out


def test_python_warning_text_is_routed_through_closed_json_boundary(capsys):
    configure_json_logging("INFO")

    warnings.warn(
        "patient Jane MRN=synthetic-123",
        RuntimeWarning,
        stacklevel=1,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Jane" not in captured.out
    assert "synthetic-123" not in captured.out
    event = json.loads(captured.out)
    assert event == {
        "event": SERVICE_LOG_EVENT,
        "logger": "service",
        "level": "WARNING",
    }


def test_handler_failure_cannot_echo_raw_record_to_stderr(capsys):
    configure_json_logging("INFO")

    class BrokenStream:
        def write(self, _value):
            raise OSError("synthetic sink failure")

        def flush(self):
            return None

    handler = logging.StreamHandler(BrokenStream())
    handler.setFormatter(PHISafeServiceFormatter())
    logger = logging.Logger("azure.synthetic", level=logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)

    logger.error(
        "patient %s MRN=%s",
        "Jane",
        "synthetic-123",
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Jane" not in captured.out
    assert "synthetic-123" not in captured.out
