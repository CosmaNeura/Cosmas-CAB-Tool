"""PHI-safe runtime logging and HTTP correlation.

Operational logs are an intentionally closed data contract.  The formatters in
this module never render a log message, traceback, exception message, request
target, header, body, or arbitrary ``extra`` field.  New observability fields
must be explicitly admitted and validated here before they can reach a sink.

``PHISafeHTTPMiddleware`` is pure ASGI so the core billing package remains
importable without the optional API dependencies.
"""
from __future__ import annotations

import json
import logging
import logging.config
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any


HTTP_LOGGER_NAME = "cosmaneura_billing.http"
HTTP_LOG_EVENT = "http_request"
LLM_LOGGER_NAME = "cosmaneura_billing.llm"
LLM_LOG_EVENT = "llm_request"
CHASE_WORKER_LOGGER_NAME = "cosmaneura_billing.chase.worker"
CHASE_WORKER_LOG_EVENT = "chase_worker_run"
AZURE_OPENAI_ACCEPTANCE_LOG_EVENT = "azure_openai_acceptance"
TA4H_SHADOW_ACCEPTANCE_LOG_EVENT = "ta4h_shadow_acceptance"
PACKAGE_LOGGER_NAME = "cosmaneura_billing"
SERVICE_LOG_EVENT = "service_event"
REQUEST_ID_HEADER = "X-Request-ID"
UNMATCHED_ROUTE = "<unmatched>"

_HTTP_FIELDS = frozenset({
    "event",
    "request_id",
    "method",
    "route",
    "status",
    "duration_ms",
    "exception",
})
_SAFE_HTTP_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
})
_ROUTE_TEMPLATE_RE = re.compile(r"/[A-Za-z0-9_./{}-]{0,255}\Z")
_SAFE_EXCEPTION_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SAFE_LOGGER_ROOTS = (
    "cosmaneura_billing", "uvicorn", "azure", "httpx", "httpcore",
)
# Admit only documented OpenAI model-family shapes used for the billing
# inference deployment. The Azure deployment name is deliberately not logged;
# this label is the provider model identifier and cannot carry free-form data.
_SAFE_LLM_MODEL_RE = re.compile(
    r"(?:"
    r"gpt-(?:4o|[4-9](?:\.[0-9]+)*)(?:-(?:mini|nano|pro|chat|sol|terra|luna))?"
    r"|o[1-9](?:-(?:mini|pro))?"
    r")(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?\Z"
)
_LLM_STATUSES = frozenset({
    "http_error",
    "invalid_response",
    "network_error",
    "retryable_http_error",
    "success",
    "terminal_response",
    "unexpected_error",
})
_LLM_FIELDS = frozenset({
    "event",
    "provider",
    "model",
    "status",
    "attempt",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
})
_CHASE_WORKER_FIELDS = frozenset({
    "event",
    "status",
    "quarantined",
    "ambiguous",
    "scheduled",
    "reserved",
    "claimed",
    "accepted",
    "permanent_failures",
})
_CHASE_WORKER_STATUSES = frozenset({
    "completed",
    "configuration_error",
    "database_uncertain",
    "transport_uncertain",
})
_AZURE_OPENAI_ACCEPTANCE_CHECKS = frozenset({
    "configuration",
    "forbidden_credentials_absent",
    "json_object_fallback",
    "managed_identity_client_routing",
    "output_limit_terminal",
    "private_dns",
    "safety_safe_outcome",
    "safety_terminal_evidence",
    "strict_structured_output",
    "summary",
})
_TA4H_SHADOW_ACCEPTANCE_CHECKS = frozenset({
    "configuration",
    "forbidden_credentials_absent",
    "managed_identity_client_routing",
    "no_influence",
    "observability_schema",
    "private_dns",
    "redirect_policy",
    "summary",
    "synthetic_analysis",
})
_CONTROLLED_LOGGERS = (
    PACKAGE_LOGGER_NAME,
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "py.warnings",
    "azure",
    "httpx",
    "httpcore",
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _opaque_request_uuid(value: object) -> uuid.UUID | None:
    """Return a version-four request UUID, never an arbitrary caller label."""

    if not isinstance(value, str) or len(value) not in {32, 36}:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        return None
    if value not in {parsed.hex, str(parsed)}:
        return None
    return parsed


def is_valid_request_id(value: object) -> bool:
    """Whether *value* is an opaque UUID4 correlation token.

    Free-form request IDs are not accepted even when printable: a patient or
    encounter identifier must not become "safe" merely because a caller placed
    it in ``X-Request-ID``.
    """

    return _opaque_request_uuid(value) is not None


def resolve_request_id(candidate: object = None) -> str:
    """Keep a canonical UUID4 candidate or generate a fresh opaque ID."""

    parsed = _opaque_request_uuid(candidate)
    return parsed.hex if parsed is not None else uuid.uuid4().hex


def request_id_from_scope(scope: Mapping[str, Any]) -> str:
    """Generate a server-owned request ID without inspecting any header.

    Even a syntactically valid UUID supplied by a caller may be a patient or
    encounter identifier.  It must not become a joinable operational-log field
    merely because it arrived under ``X-Request-ID``.
    """

    del scope
    return resolve_request_id()


def _safe_method(value: object) -> str:
    if not isinstance(value, str):
        return "UNKNOWN"
    candidate = value.upper()
    return candidate if candidate in _SAFE_HTTP_METHODS else "UNKNOWN"


def _safe_route_template(value: object) -> str:
    if (
        not isinstance(value, str)
        or "?" in value
        or "#" in value
        or not _ROUTE_TEMPLATE_RE.fullmatch(value)
    ):
        return UNMATCHED_ROUTE
    return re.sub(r"/{2,}", "/", value) or "/"


def normalized_route_template(scope: Mapping[str, Any]) -> str:
    """Return only a declared route template, never the request target."""

    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return _safe_route_template(template)


def _safe_status(value: object) -> int:
    return value if type(value) is int and 100 <= value <= 599 else 500


def _safe_duration(value: object) -> float:
    if type(value) not in {int, float}:
        return 0.0
    candidate = float(value)
    if not math.isfinite(candidate) or candidate < 0:
        return 0.0
    return round(min(candidate, 86_400_000.0), 3)


def _safe_exception_name(value: object) -> str | None:
    if isinstance(value, BaseException):
        value = type(value).__name__
    if isinstance(value, str) and _SAFE_EXCEPTION_RE.fullmatch(value):
        return value
    return None


def _exception_class_from_record(record: logging.LogRecord) -> str | None:
    explicit = _safe_exception_name(getattr(record, "exception", None))
    if explicit is not None:
        return explicit
    if record.exc_info and isinstance(record.exc_info, tuple) and record.exc_info[0]:
        return _safe_exception_name(getattr(record.exc_info[0], "__name__", None))
    return None


class PHISafeJSONFormatter(logging.Formatter):
    """Serialize a fixed, validated HTTP event without formatting the message."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "event": HTTP_LOG_EVENT,
            "request_id": resolve_request_id(getattr(record, "request_id", None)),
            "method": _safe_method(getattr(record, "method", None)),
            "route": _safe_route_template(getattr(record, "route", None)),
            "status": _safe_status(getattr(record, "status", None)),
            "duration_ms": _safe_duration(getattr(record, "duration_ms", None)),
        }
        exception_name = _exception_class_from_record(record)
        if exception_name is not None:
            payload["exception"] = exception_name
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class PHISafeServiceFormatter(logging.Formatter):
    """Last-resort boundary for framework and SDK loggers.

    Framework and SDK messages are deliberately excluded because they can
    contain raw URLs, recipients, headers, provider payloads, or exception
    messages.  Logger name, severity, and exception *class* retain enough signal
    to alert without serializing the unsafe diagnostic object.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Emit only a fixed namespace, not an arbitrary logger name.  A logger
        # created from a patient or resource label must not turn that label into
        # an allowed diagnostic field.
        logger_name = "service"
        if isinstance(record.name, str):
            for root in _SAFE_LOGGER_ROOTS:
                if record.name == root or record.name.startswith(f"{root}."):
                    logger_name = root
                    break
        level = record.levelname if record.levelname in {
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
        } else "INFO"
        payload: dict[str, Any] = {
            "event": SERVICE_LOG_EVENT,
            "logger": logger_name,
            "level": level,
        }
        exception_name = _exception_class_from_record(record)
        if exception_name is not None:
            payload["exception"] = exception_name
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _safe_metric_count(value: object, *, maximum: int) -> int | None:
    if type(value) is not int or not 0 <= value <= maximum:
        return None
    return value


def safe_llm_model_label(value: object) -> str:
    """Return only an approved OpenAI model identifier shape."""

    if isinstance(value, str) and _SAFE_LLM_MODEL_RE.fullmatch(value):
        return value
    return "invalid_model_label"


class LLMMetricJSONFormatter(logging.Formatter):
    """Serialize only validated, non-clinical LLM cost/latency metrics.

    Token counts and retry latency are needed for pilot pricing.  Prompt,
    response, request correlation, exception data, and arbitrary extras are not
    admitted to this schema.
    """

    def format(self, record: logging.LogRecord) -> str:
        model = safe_llm_model_label(getattr(record, "model", None))
        status = getattr(record, "status", None)
        if status not in _LLM_STATUSES:
            status = "unexpected_error"
        attempt = _safe_metric_count(getattr(record, "attempt", None), maximum=100)
        if attempt in {None, 0}:
            attempt = 1
        payload = {
            "event": LLM_LOG_EVENT,
            "provider": (
                "azure_openai"
                if getattr(record, "provider", None) == "azure_openai"
                else "unknown"
            ),
            "model": model,
            "status": status,
            "attempt": attempt,
            "latency_ms": _safe_metric_count(
                getattr(record, "latency_ms", None), maximum=86_400_000,
            ),
            "input_tokens": _safe_metric_count(
                getattr(record, "input_tokens", None), maximum=1_000_000_000,
            ),
            "output_tokens": _safe_metric_count(
                getattr(record, "output_tokens", None), maximum=1_000_000_000,
            ),
            "cache_creation_input_tokens": _safe_metric_count(
                getattr(record, "cache_creation_input_tokens", None),
                maximum=1_000_000_000,
            ),
            "cache_read_input_tokens": _safe_metric_count(
                getattr(record, "cache_read_input_tokens", None),
                maximum=1_000_000_000,
            ),
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class ChaseWorkerRunJSONFormatter(logging.Formatter):
    """Serialize one bounded worker result without a patient-joinable ID."""

    def format(self, record: logging.LogRecord) -> str:
        status = getattr(record, "status", None)
        if status not in _CHASE_WORKER_STATUSES:
            status = "configuration_error"
        payload = {
            "event": CHASE_WORKER_LOG_EVENT,
            "status": status,
        }
        for field in (
            "quarantined",
            "ambiguous",
            "scheduled",
            "reserved",
            "claimed",
            "accepted",
            "permanent_failures",
        ):
            value = _safe_metric_count(getattr(record, field, None), maximum=1_000_000)
            payload[field] = 0 if value is None else value
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def json_logging_config(level: str | int = "INFO") -> dict[str, Any]:
    """Return a stdlib logging configuration with one PHI-safe sink boundary."""

    return {
        "version": 1,
        # Existing third-party handlers can format raw SDK exception objects
        # before a root filter sees them.  Disable them; explicitly configured
        # namespaces below re-enter through a closed formatter.
        "disable_existing_loggers": True,
        "formatters": {
            "phi_safe_http": {"()": PHISafeJSONFormatter},
            "phi_safe_service": {"()": PHISafeServiceFormatter},
            "phi_safe_llm": {"()": LLMMetricJSONFormatter},
            "phi_safe_chase_worker": {"()": ChaseWorkerRunJSONFormatter},
        },
        "handlers": {
            "discard": {
                # An empty non-propagating logger falls through to Python's
                # lastResort stderr handler at WARNING+, which would re-expose
                # Uvicorn's raw request target.  NullHandler consumes it.
                "class": "logging.NullHandler",
            },
            "phi_safe_http": {
                "class": "logging.StreamHandler",
                "formatter": "phi_safe_http",
                "stream": "ext://sys.stdout",
            },
            "phi_safe_service": {
                "class": "logging.StreamHandler",
                "formatter": "phi_safe_service",
                "stream": "ext://sys.stdout",
            },
            "phi_safe_llm": {
                "class": "logging.StreamHandler",
                "formatter": "phi_safe_llm",
                "stream": "ext://sys.stdout",
            },
            "phi_safe_chase_worker": {
                "class": "logging.StreamHandler",
                "formatter": "phi_safe_chase_worker",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "handlers": ["phi_safe_service"],
            "level": level,
        },
        "loggers": {
            PACKAGE_LOGGER_NAME: {
                "handlers": ["phi_safe_service"],
                "level": level,
                "propagate": False,
            },
            HTTP_LOGGER_NAME: {
                "handlers": ["phi_safe_http"],
                # Request accounting is an operational invariant, not debug
                # verbosity.  Keep successful requests at INFO even when SDK
                # and service diagnostics are raised to WARNING or ERROR.
                "level": "INFO",
                "propagate": False,
            },
            LLM_LOGGER_NAME: {
                "handlers": ["phi_safe_llm"],
                # Every provider attempt can be billable, including successes.
                "level": "INFO",
                "propagate": False,
            },
            CHASE_WORKER_LOGGER_NAME: {
                "handlers": ["phi_safe_chase_worker"],
                "level": "INFO",
                "propagate": False,
            },
            # Uvicorn configures this parent with a raw DefaultFormatter before
            # application startup.  Replacing only its named children leaves
            # that handler reachable by any later-created ``uvicorn.*`` logger.
            "uvicorn": {
                "handlers": ["phi_safe_service"],
                "level": level,
                "propagate": False,
            },
            # Conventional access lines contain the raw request target.
            "uvicorn.access": {
                "handlers": ["discard"],
                "level": "CRITICAL",
                "propagate": False,
            },
            # Framework/transport SDK messages are reduced to safe metadata.
            "uvicorn.error": {
                "handlers": ["phi_safe_service"],
                "level": level,
                "propagate": False,
            },
            # ``logging.captureWarnings`` routes caller- and dependency-owned
            # warning text here.  The service formatter deliberately discards
            # the message and retains only fixed metadata.
            "py.warnings": {
                "handlers": ["phi_safe_service"],
                "level": "WARNING",
                "propagate": False,
            },
            "azure": {
                "handlers": ["phi_safe_service"],
                "level": "WARNING",
                "propagate": False,
            },
            "httpx": {
                "handlers": ["phi_safe_service"],
                "level": "WARNING",
                "propagate": False,
            },
            "httpcore": {
                "handlers": ["phi_safe_service"],
                "level": "WARNING",
                "propagate": False,
            },
        },
    }


def configure_json_logging(level: str | int = "INFO") -> logging.Logger:
    """Install PHI-safe logging and return the dedicated HTTP logger."""

    logging.config.dictConfig(json_logging_config(level))
    # Handler failures must never trigger logging's diagnostic fallback: that
    # fallback renders the original caller-owned message and args to stderr.
    logging.raiseExceptions = False
    # Warnings otherwise bypass logging entirely and write their caller-owned
    # message to stderr.  Route them through the closed service formatter.
    # Re-arm even if another framework replaced ``warnings.showwarning`` after
    # an earlier configuration call; ``captureWarnings(True)`` alone is a
    # no-op while its private saved hook remains populated.
    logging.captureWarnings(False)
    logging.captureWarnings(True)
    # ``dictConfig`` normally resets children of named loggers, but enforce it
    # explicitly so an SDK-created child cannot retain a pre-configuration raw
    # handler on Python/runtime variants with different incremental behavior.
    _sanitize_preexisting_child_loggers()
    return logging.getLogger(HTTP_LOGGER_NAME)


def _sanitize_preexisting_child_loggers() -> None:
    """Remove raw handlers from children of every controlled namespace."""

    configured = {
        PACKAGE_LOGGER_NAME,
        HTTP_LOGGER_NAME,
        LLM_LOGGER_NAME,
        CHASE_WORKER_LOGGER_NAME,
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "py.warnings",
        "azure",
        "httpx",
        "httpcore",
    }
    for logger_name, candidate in logging.root.manager.loggerDict.items():
        if not isinstance(candidate, logging.Logger):
            continue
        if (
            logger_name not in configured
            and any(
                logger_name.startswith(f"{root}.")
                for root in _CONTROLLED_LOGGERS
            )
        ):
            candidate.handlers.clear()
            candidate.filters.clear()
            candidate.propagate = True
            candidate.disabled = False


def generic_error_payload(request_id: str) -> dict[str, str]:
    """Build a non-diagnostic public 500 response payload."""

    return {
        "detail": "Internal server error",
        "requestId": resolve_request_id(request_id),
    }


async def _send_generic_500(send: Send, request_id: str) -> None:
    body = json.dumps(
        generic_error_payload(request_id), separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    await send({
        "type": "http.response.start",
        "status": 500,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-request-id", request_id.encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class PHISafeHTTPMiddleware:
    """Emit bounded access JSON, attach a request ID, and genericize failures."""

    def __init__(self, app: ASGIApp, *, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.logger = logger or logging.getLogger(HTTP_LOGGER_NAME)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        request_id = request_id_from_scope(scope)
        state = scope.setdefault("state", {})
        if isinstance(state, MutableMapping):
            state["request_id"] = request_id

        status = 500
        response_started = False
        exception_class: str | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status
            if message.get("type") == "http.response.start":
                response_started = True
                status = _safe_status(message.get("status"))
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if isinstance(name, bytes) and name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            exception_class = _safe_exception_name(exc) or "Exception"
            status = 500
            if response_started:
                raise
            await _send_generic_500(send, request_id)
        finally:
            event: dict[str, Any] = {
                "event": HTTP_LOG_EVENT,
                "request_id": request_id,
                "method": _safe_method(scope.get("method")),
                "route": normalized_route_template(scope),
                "status": status,
                "duration_ms": round(
                    (time.perf_counter() - started_at) * 1000, 3,
                ),
            }
            if exception_class is not None:
                event["exception"] = exception_class
            self.logger.log(
                logging.ERROR if exception_class else logging.INFO,
                HTTP_LOG_EVENT,
                extra=event,
            )


def is_safe_http_event(payload: object) -> bool:
    """Validate a rendered HTTP event without accepting additional fields."""

    if not isinstance(payload, dict) or not set(payload) <= _HTTP_FIELDS:
        return False
    required = _HTTP_FIELDS - {"exception"}
    if set(payload) not in {required, _HTTP_FIELDS}:
        return False
    return (
        payload.get("event") == HTTP_LOG_EVENT
        and is_valid_request_id(payload.get("request_id"))
        and _safe_method(payload.get("method")) == payload.get("method")
        and _safe_route_template(payload.get("route")) == payload.get("route")
        and _safe_status(payload.get("status")) == payload.get("status")
        and _safe_duration(payload.get("duration_ms"))
        == payload.get("duration_ms")
        and (
            "exception" not in payload
            or _safe_exception_name(payload.get("exception"))
            == payload.get("exception")
        )
    )


def is_safe_llm_event(payload: object) -> bool:
    """Validate one rendered LLM metric without allowing schema expansion."""

    if not isinstance(payload, dict) or set(payload) != _LLM_FIELDS:
        return False
    return (
        payload.get("event") == LLM_LOG_EVENT
        and payload.get("provider") in {"azure_openai", "unknown"}
        and isinstance(payload.get("model"), str)
        and (
            payload.get("model") == "invalid_model_label"
            or _SAFE_LLM_MODEL_RE.fullmatch(payload["model"]) is not None
        )
        and payload.get("status") in _LLM_STATUSES
        and type(payload.get("attempt")) is int
        and 1 <= payload["attempt"] <= 100
        and all(
            payload.get(field) is None
            or (
                type(payload[field]) is int
                and 0 <= payload[field] <= maximum
            )
            for field, maximum in (
                ("latency_ms", 86_400_000),
                ("input_tokens", 1_000_000_000),
                ("output_tokens", 1_000_000_000),
                ("cache_creation_input_tokens", 1_000_000_000),
                ("cache_read_input_tokens", 1_000_000_000),
            )
        )
    )


def is_safe_chase_worker_event(payload: object) -> bool:
    """Validate one terminal worker event without accepting identifiers."""

    if not isinstance(payload, dict) or set(payload) != _CHASE_WORKER_FIELDS:
        return False
    return (
        payload.get("event") == CHASE_WORKER_LOG_EVENT
        and payload.get("status") in _CHASE_WORKER_STATUSES
        and all(
            type(payload.get(field)) is int
            and 0 <= payload[field] <= 1_000_000
            for field in _CHASE_WORKER_FIELDS - {"event", "status"}
        )
    )


def is_safe_azure_openai_acceptance_event(payload: object) -> bool:
    """Validate one synthetic acceptance event with no free-form fields.

    The acceptance harness deliberately emits no endpoint, resource identifier,
    prompt, response, exception, or credential-shaped value.  A check event has
    exactly three bounded strings; the terminal summary adds only small counts.
    """

    if not isinstance(payload, dict):
        return False
    if payload.get("event") != AZURE_OPENAI_ACCEPTANCE_LOG_EVENT:
        return False
    check = payload.get("check")
    status = payload.get("status")
    if check == "summary":
        if set(payload) != {
            "event", "check", "status", "passed", "failed", "skipped",
        }:
            return False
        return (
            status in {"pass", "fail"}
            and all(
                type(payload.get(field)) is int
                and 0 <= payload[field] <= 100
                for field in ("passed", "failed", "skipped")
            )
        )
    return (
        set(payload) == {"event", "check", "status"}
        and check in _AZURE_OPENAI_ACCEPTANCE_CHECKS - {"summary"}
        and status in {"pass", "fail", "skip"}
    )


def is_safe_ta4h_shadow_acceptance_event(payload: object) -> bool:
    """Validate the fixed synthetic shadow-deployment acceptance schema."""

    if not isinstance(payload, dict):
        return False
    if payload.get("event") != TA4H_SHADOW_ACCEPTANCE_LOG_EVENT:
        return False
    check = payload.get("check")
    status = payload.get("status")
    if check == "summary":
        if set(payload) != {
            "event", "check", "status", "passed", "failed", "skipped",
        }:
            return False
        return (
            status in {"pass", "fail"}
            and all(
                type(payload.get(field)) is int
                and 0 <= payload[field] <= 100
                for field in ("passed", "failed", "skipped")
            )
        )
    return (
        set(payload) == {"event", "check", "status"}
        and check in _TA4H_SHADOW_ACCEPTANCE_CHECKS - {"summary"}
        and status in {"pass", "fail", "skip"}
    )


__all__ = [
    "AZURE_OPENAI_ACCEPTANCE_LOG_EVENT",
    "HTTP_LOG_EVENT",
    "HTTP_LOGGER_NAME",
    "LLM_LOG_EVENT",
    "LLM_LOGGER_NAME",
    "LLMMetricJSONFormatter",
    "CHASE_WORKER_LOG_EVENT",
    "CHASE_WORKER_LOGGER_NAME",
    "ChaseWorkerRunJSONFormatter",
    "PACKAGE_LOGGER_NAME",
    "PHISafeHTTPMiddleware",
    "PHISafeJSONFormatter",
    "PHISafeServiceFormatter",
    "REQUEST_ID_HEADER",
    "SERVICE_LOG_EVENT",
    "TA4H_SHADOW_ACCEPTANCE_LOG_EVENT",
    "UNMATCHED_ROUTE",
    "configure_json_logging",
    "generic_error_payload",
    "is_safe_http_event",
    "is_safe_azure_openai_acceptance_event",
    "is_safe_ta4h_shadow_acceptance_event",
    "is_safe_chase_worker_event",
    "is_safe_llm_event",
    "is_valid_request_id",
    "json_logging_config",
    "normalized_route_template",
    "request_id_from_scope",
    "resolve_request_id",
    "safe_llm_model_label",
]
