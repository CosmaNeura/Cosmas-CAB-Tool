"""Fail-closed scanner for captured operational logs.

The scanner reports only finding codes and counts.  It never repeats a matched
value, log line, or file path because the scanner's own output is also a log.
Use ``prohibited_values`` for synthetic CI canaries, including opaque patient
identifiers that cannot be identified safely by their shape alone.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence

from .runtime import (
    CHASE_WORKER_LOG_EVENT,
    HTTP_LOG_EVENT,
    LLM_LOG_EVENT,
    SERVICE_LOG_EVENT,
    is_safe_http_event,
    is_safe_chase_worker_event,
    is_safe_llm_event,
)


_FORBIDDEN_FIELDS = frozenset({
    "authorization",
    "body",
    "clinical_note",
    "content",
    "cookie",
    "diagnosis",
    "encounter_id",
    "error_message",
    "evidence",
    "exception_message",
    "header",
    "headers",
    "message",
    "note",
    "notes",
    "patient",
    "patient_id",
    "patient_name",
    "patient_ref",
    "prompt",
    "query",
    "query_string",
    "raw_path",
    "recipient",
    "request_body",
    "response_body",
    "sdk_error",
    "subject",
    "token",
    "traceback",
    "transcript",
})
_SECRET_FIELD_PARTS = frozenset({
    "access_key",
    "access_token",
    "api_key",
    "client_secret",
    "connection_string",
    "password",
    "refresh_token",
    "sas_token",
    "secret",
})
_SERVICE_FIELDS = frozenset({
    "event", "logger", "level", "exception",
})
_SERVICE_LOGGERS = frozenset({
    "azure", "cosmaneura_billing", "httpcore", "httpx", "service", "uvicorn",
})
_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_EXCEPTION_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SCAN_EVENT = "phi_log_scan"
_FINDING_CODES = frozenset({
    "email_address",
    "forbidden_assignment",
    "forbidden_field",
    "invalid_http_schema",
    "invalid_llm_schema",
    "invalid_worker_schema",
    "invalid_schema",
    "invalid_service_schema",
    "jwt",
    "labelled_patient_identifier",
    "log_read_failed",
    "phone_number",
    "prohibited_value",
    "raw_request_target",
    "secret_marker",
    "social_security_number",
    "unknown_event",
    "unstructured_log",
})

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "secret_marker",
        re.compile(
            r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+|"
            r"sharedaccesskey(?:name)?\s*=|"
            r"(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|"
            r"connection[_ -]?string|password|refresh[_ -]?token|"
            r"sas[_ -]?token)\s*[:=])"
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "email_address",
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b"),
    ),
    (
        "social_security_number",
        re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    ),
    (
        "phone_number",
        re.compile(
            r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})"
            r"[ .-]\d{3}[ .-]\d{4}(?![\w-])"
        ),
    ),
    (
        "labelled_patient_identifier",
        re.compile(
            r"(?i)\b(?:dob|date[_ -]?of[_ -]?birth|mrn|medical[_ -]?record"
            r"(?:[_ -]?number)?|patient[_ -]?(?:id|ref|name))\s*[:=]"
        ),
    ),
    (
        "raw_request_target",
        re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+/\S+"),
    ),
    (
        "forbidden_assignment",
        re.compile(
            r"(?i)\b(?:body|clinical[_ -]?note|evidence|headers?|notes?|prompt|"
            r"query(?:[_ -]?string)?|raw[_ -]?path|recipient|sdk[_ -]?error|"
            r"subject|transcript)\s*[:=]"
        ),
    ),
)


@dataclass(frozen=True, order=True)
class LogFinding:
    """A non-sensitive scanner result."""

    line_number: int
    code: str


def _normalized_field(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _contains_forbidden_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            field = _normalized_field(key)
            if (
                field in _FORBIDDEN_FIELDS
                or any(part in field for part in _SECRET_FIELD_PARTS)
            ):
                return True
            if _contains_forbidden_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_field(item) for item in value)
    return False


def _is_safe_service_event(payload: object) -> bool:
    if not isinstance(payload, dict) or not set(payload) <= _SERVICE_FIELDS:
        return False
    if not {"event", "logger", "level"} <= set(payload):
        return False
    logger_name = payload.get("logger")
    exception_name = payload.get("exception")
    return (
        payload.get("event") == SERVICE_LOG_EVENT
        and logger_name in _SERVICE_LOGGERS
        and payload.get("level") in _LEVELS
        and (
            exception_name is None
            or (
                isinstance(exception_name, str)
                and _EXCEPTION_RE.fullmatch(exception_name) is not None
            )
        )
    )


def _is_safe_scan_event(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("event") != _SCAN_EVENT:
        return False
    if set(payload) == {"event", "status"}:
        return payload.get("status") in {"passed", "failed"}
    if set(payload) != {"event", "status", "finding_count", "codes"}:
        return False
    count = payload.get("finding_count")
    codes = payload.get("codes")
    return (
        payload.get("status") in {"passed", "failed"}
        and type(count) is int
        and 0 <= count <= 1_000_000
        and isinstance(codes, list)
        and len(codes) <= len(_FINDING_CODES)
        and all(code in _FINDING_CODES for code in codes)
    )


def scan_log_text(
    text: str,
    *,
    prohibited_values: Iterable[str] = (),
) -> list[LogFinding]:
    """Return PHI/secret/schema findings without retaining matched content."""

    canaries = tuple(
        value.casefold()
        for value in prohibited_values
        if isinstance(value, str) and value
    )
    findings: set[LogFinding] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        folded = line.casefold()
        if any(canary in folded for canary in canaries):
            findings.add(LogFinding(line_number, "prohibited_value"))
        for code, pattern in _PATTERNS:
            if pattern.search(line):
                findings.add(LogFinding(line_number, code))

        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            findings.add(LogFinding(line_number, "unstructured_log"))
            continue
        if _contains_forbidden_field(payload):
            findings.add(LogFinding(line_number, "forbidden_field"))
        if not isinstance(payload, dict):
            findings.add(LogFinding(line_number, "invalid_schema"))
            continue
        event = payload.get("event")
        if event == HTTP_LOG_EVENT:
            if not is_safe_http_event(payload):
                findings.add(LogFinding(line_number, "invalid_http_schema"))
        elif event == LLM_LOG_EVENT:
            if not is_safe_llm_event(payload):
                findings.add(LogFinding(line_number, "invalid_llm_schema"))
        elif event == CHASE_WORKER_LOG_EVENT:
            if not is_safe_chase_worker_event(payload):
                findings.add(LogFinding(line_number, "invalid_worker_schema"))
        elif event == SERVICE_LOG_EVENT:
            if not _is_safe_service_event(payload):
                findings.add(LogFinding(line_number, "invalid_service_schema"))
        elif event == _SCAN_EVENT:
            if not _is_safe_scan_event(payload):
                findings.add(LogFinding(line_number, "invalid_schema"))
        else:
            findings.add(LogFinding(line_number, "unknown_event"))
    return sorted(findings)


def scan_log_paths(
    paths: Sequence[Path],
    *,
    prohibited_values: Iterable[str] = (),
) -> list[LogFinding]:
    """Scan files while keeping paths and content out of returned findings."""

    findings: list[LogFinding] = []
    line_offset = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(LogFinding(line_offset + 1, "log_read_failed"))
            line_offset += 1
            continue
        current = scan_log_text(text, prohibited_values=prohibited_values)
        findings.extend(
            LogFinding(item.line_number + line_offset, item.code)
            for item in current
        )
        line_offset += max(1, len(text.splitlines()))
    return sorted(set(findings))


def _self_test() -> bool:
    patient_canary = "70000000-0000-4000-8000-000000000007"
    unsafe = json.dumps({
        "event": HTTP_LOG_EVENT,
        "patient_id": patient_canary,
        "authorization": "Bearer synthetic-ci-secret",
    })
    codes = {
        finding.code
        for finding in scan_log_text(
            unsafe, prohibited_values=[patient_canary],
        )
    }
    return {
        "forbidden_field", "prohibited_value", "secret_marker",
        "invalid_http_schema",
    } <= codes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed when captured operational logs contain unsafe data.",
    )
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--prohibited-value", action="append", default=[])
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args(argv)

    if arguments.self_test:
        passed = _self_test()
        print(json.dumps({
            "event": _SCAN_EVENT,
            "status": "passed" if passed else "failed",
        }, separators=(",", ":")))
        return 0 if passed else 1
    if not arguments.paths:
        parser.error("at least one captured log path is required")

    findings = scan_log_paths(
        arguments.paths,
        prohibited_values=arguments.prohibited_value,
    )
    print(json.dumps({
        "event": _SCAN_EVENT,
        "status": "failed" if findings else "passed",
        "finding_count": len(findings),
        "codes": sorted({finding.code for finding in findings}),
    }, separators=(",", ":")))
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())


__all__ = [
    "LogFinding",
    "main",
    "scan_log_paths",
    "scan_log_text",
]
