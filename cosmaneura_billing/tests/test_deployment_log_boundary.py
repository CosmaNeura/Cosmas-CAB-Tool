"""Route and deployed-log evidence guards for the platform logging boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import cosmaneura_billing.api as api
from cosmaneura_billing.log_scanner import (
    DEPLOYMENT_LOG_QUERY_ID,
    DEPLOYMENT_LOG_STREAMS,
    main,
    scan_deployment_count_report,
    scan_log_text,
)
from cosmaneura_billing.settings import Settings


RESOURCE_CANARY = "patient-path-canary-70000007"
ROOT = Path(__file__).resolve().parents[2]


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def log(
        self, _level: int, _message: str, *, extra: dict[str, object],
    ) -> None:
        self.events.append(extra)


def _count_report(**matches: int) -> dict[str, object]:
    return {
        "schema_version": 2,
        "canary_count": 5,
        "scope": {
            "query_id": DEPLOYMENT_LOG_QUERY_ID,
            "canary_set_sha256": "0" * 64,
            "image_digest": "sha256:" + "1" * 64,
            "api_revision": "cosmaneura-api-0000001",
            "window_start": "2026-08-20T17:00:00Z",
            "window_end": "2026-08-20T17:30:00Z",
            "migration_execution_ids": ["migration-0000001"],
            "chase_worker_execution_ids": ["worker-0000001"],
        },
        "streams": [
            {
                "source": source,
                "row_count": 1,
                "prohibited_match_count": matches.get(source, 0),
            }
            for source in sorted(DEPLOYMENT_LOG_STREAMS)
        ],
    }


def _codes(payload: object) -> set[str]:
    return {item.code for item in scan_deployment_count_report(payload)}


def test_production_api_routes_never_put_resource_identifiers_in_paths():
    api_routes = [
        route for route in api.router.routes
        if getattr(route, "path", "").startswith("/api/")
    ]

    assert api_routes
    assert all(not getattr(route, "param_convertors", {}) for route in api_routes)
    assert all("{" not in route.path and "}" not in route.path for route in api_routes)
    assert all(not route.dependant.query_params for route in api_routes)


def test_encounter_identifier_in_body_never_reaches_runtime_log(
    monkeypatch, tmp_path,
):
    logger = _RecordingLogger()
    monkeypatch.setattr(api, "configure_json_logging", lambda _level: logger)
    application = api.create_app(
        settings=Settings(), frontend_dist=tmp_path / "missing-frontend",
    )

    response = TestClient(application).post(
        "/api/encounters/detail", json={"encounterId": RESOURCE_CANARY},
    )

    assert response.status_code == 404
    assert len(logger.events) == 1
    event = logger.events[0]
    assert event["route"] == "/api/encounters/detail"
    rendered = json.dumps({"event": event, "response": response.json()})
    assert RESOURCE_CANARY not in rendered


def test_deployment_count_report_requires_every_console_and_sidecar_stream():
    assert scan_deployment_count_report(_count_report()) == []

    missing_sidecar = _count_report()
    missing_sidecar["streams"] = [
        stream for stream in missing_sidecar["streams"]
        if stream["source"] != "http-auth-console"
    ]
    assert "missing_deployment_log_stream" in _codes(missing_sidecar)


def test_operator_contract_names_every_required_azure_log_source():
    operator_guide = (ROOT / "infra" / "README.md").read_text(encoding="utf-8")

    assert "ContainerAppConsoleLogs_CL" in operator_guide
    assert "ContainerAppSystemLogs_CL" in operator_guide
    assert "--deployment-count-report" in operator_guide
    for source in DEPLOYMENT_LOG_STREAMS:
        assert f"`{source}`" in operator_guide


def test_deployment_count_report_fails_on_empty_or_prohibited_streams():
    empty_job = _count_report()
    next(
        stream for stream in empty_job["streams"]
        if stream["source"] == "chase-worker-job-console"
    )["row_count"] = 0
    assert "empty_deployment_log_stream" in _codes(empty_job)

    sidecar_match = _count_report(**{"http-auth-console": 1})
    assert "prohibited_value" in _codes(sidecar_match)


def test_deployment_count_report_rejects_malformed_counts_without_crashing():
    malformed = _count_report()
    malformed["streams"][0]["source"] = ["api-console"]
    malformed["streams"][1]["row_count"] = True

    assert "invalid_deployment_scan_schema" in _codes(malformed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query_id", "unknown-query"),
        ("canary_set_sha256", "not-a-digest"),
        ("image_digest", "sha256:" + "z" * 64),
        ("api_revision", "contains spaces"),
        ("window_end", "2026-08-20T16:59:59Z"),
        ("migration_execution_ids", []),
        ("chase_worker_execution_ids", ["duplicate", "duplicate"]),
    ],
)
def test_deployment_count_report_is_bound_to_exact_acceptance_scope(field, value):
    report = _count_report()
    report["scope"][field] = value

    assert "invalid_deployment_scan_schema" in _codes(report)


def test_deployment_count_cli_emits_only_codes_and_counts(tmp_path, capsys):
    report_path = tmp_path / "private-canary-report.json"
    report_path.write_text(
        json.dumps(_count_report(**{"http-auth-console": 1})),
        encoding="utf-8",
    )

    assert main(["--deployment-count-report", str(report_path)]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result == {
        "event": "phi_log_scan",
        "status": "failed",
        "finding_count": 1,
        "codes": ["prohibited_value"],
    }
    assert scan_log_text(output) == []
    for prohibited in (*DEPLOYMENT_LOG_STREAMS, str(report_path)):
        assert prohibited not in output


def test_self_test_cannot_mask_an_unvalidated_deployment_report(tmp_path):
    report_path = tmp_path / "deployment-log-counts.json"
    report_path.write_text(json.dumps(_count_report()), encoding="utf-8")

    with pytest.raises(SystemExit):
        main([
            "--self-test",
            "--deployment-count-report",
            str(report_path),
        ])
