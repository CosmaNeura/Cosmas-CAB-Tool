"""CI scanner for PHI and secrets in captured operational logs."""
from __future__ import annotations

import json

from cosmaneura_billing.log_scanner import main, scan_log_text
from cosmaneura_billing.runtime import (
    CHASE_WORKER_LOG_EVENT, HTTP_LOG_EVENT, LLM_LOG_EVENT, SERVICE_LOG_EVENT,
)


REQUEST_ID = "11111111111141118111111111111111"
PATIENT_ID = "70000000-0000-4000-8000-000000000007"


def _codes(text: str, *, prohibited_values=()) -> set[str]:
    return {
        finding.code
        for finding in scan_log_text(
            text, prohibited_values=prohibited_values,
        )
    }


def test_closed_http_and_service_events_pass_the_scanner():
    captured = "\n".join((
        json.dumps({
            "event": HTTP_LOG_EVENT,
            "request_id": REQUEST_ID,
            "method": "POST",
            "route": "/api/encounters/{encounter_id}/billing",
            "status": 200,
            "duration_ms": 12.5,
        }),
        json.dumps({
            "event": SERVICE_LOG_EVENT,
            "logger": "azure",
            "level": "ERROR",
            "exception": "ServiceRequestError",
        }),
        json.dumps({
            "event": LLM_LOG_EVENT,
            "provider": "azure_openai",
            "model": "gpt-4.1-mini-2025-04-14",
            "status": "success",
            "attempt": 1,
            "latency_ms": 250,
            "input_tokens": 21,
            "output_tokens": 3,
            "cache_creation_input_tokens": 8,
            "cache_read_input_tokens": 13,
        }),
        json.dumps({
            "event": CHASE_WORKER_LOG_EVENT,
            "status": "completed",
            "quarantined": 0,
            "ambiguous": 0,
            "scheduled": 4,
            "reserved": 3,
            "claimed": 2,
            "accepted": 2,
            "permanent_failures": 0,
        }),
    ))

    assert scan_log_text(captured) == []


def test_patient_identifier_canary_is_not_whitelisted_as_a_safe_uuid():
    captured = json.dumps({
        "event": HTTP_LOG_EVENT,
        "request_id": REQUEST_ID,
        "method": "GET",
        "route": "/api/encounters/{encounter_id}",
        "status": 200,
        "duration_ms": 1.0,
        "patient_id": PATIENT_ID,
    })

    codes = _codes(captured, prohibited_values=[PATIENT_ID])
    assert "prohibited_value" in codes
    assert "forbidden_field" in codes
    assert "invalid_http_schema" in codes


def test_nested_phi_and_secrets_are_detected_without_returning_values():
    captured = json.dumps({
        "event": SERVICE_LOG_EVENT,
        "logger": "azure",
        "level": "ERROR",
        "context": {
            "recipient": "jane.patient@example.invalid",
            "headers": {"Authorization": "Bearer synthetic-ci-token"},
            "clinical_note": "DOB=01/02/1980 MRN=123456",
            "sdk_error": "provider rejected raw payload",
        },
    })

    findings = scan_log_text(captured)
    codes = {finding.code for finding in findings}
    assert {
        "forbidden_field", "email_address", "secret_marker",
        "labelled_patient_identifier", "invalid_service_schema",
    } <= codes
    rendered_findings = repr(findings)
    for prohibited in (
        "jane.patient", "synthetic-ci-token", "01/02/1980", "123456",
    ):
        assert prohibited not in rendered_findings


def test_raw_targets_and_unstructured_sensitive_fields_fail_closed():
    captured = (
        "POST /api/patients/Jane-Patient?token=secret HTTP/1.1 "
        "body=private-note recipient=+1-212-555-0199"
    )

    codes = _codes(captured)
    assert {
        "raw_request_target", "forbidden_assignment", "phone_number",
        "unstructured_log",
    } <= codes


def test_unknown_or_expanded_structured_events_fail_closed():
    assert "unknown_event" in _codes(json.dumps({
        "event": "sdk_error", "status": "failed",
    }))
    assert "invalid_service_schema" in _codes(json.dumps({
        "event": SERVICE_LOG_EVENT,
        "logger": "azure.patient.Jane",
        "level": "ERROR",
    }))
    sdk_identifier = json.dumps({
        "event": SERVICE_LOG_EVENT,
        "logger": "azure",
        "level": "ERROR",
        "request_id": PATIENT_ID,
    })
    assert {
        "invalid_service_schema", "prohibited_value",
    } <= _codes(sdk_identifier, prohibited_values=[PATIENT_ID])
    assert "invalid_llm_schema" in _codes(json.dumps({
        "event": LLM_LOG_EVENT,
        "provider": "azure_openai",
        "model": "gpt-4.1-mini",
        "status": "success",
        "attempt": 1,
        "latency_ms": 1,
        "input_tokens": 2,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "prompt": "patient Alice",
    }))


def test_cli_self_test_is_safe_and_green(capsys):
    assert main(["--self-test"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "event": "phi_log_scan", "status": "passed",
    }
    assert PATIENT_ID not in output
    assert scan_log_text(output) == []


def test_cli_reports_only_codes_and_counts_not_log_content(tmp_path, capsys):
    captured = tmp_path / "patient-Jane-secret.log"
    captured.write_text(
        json.dumps({
            "event": SERVICE_LOG_EVENT,
            "logger": "azure",
            "level": "ERROR",
            "recipient": "jane.patient@example.invalid",
        }),
        encoding="utf-8",
    )

    assert main([str(captured)]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["event"] == "phi_log_scan"
    assert result["status"] == "failed"
    assert result["finding_count"] > 0
    for prohibited in (
        "Jane", "jane.patient", "example.invalid", str(captured),
    ):
        assert prohibited not in output
