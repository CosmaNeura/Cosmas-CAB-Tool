"""Contract and output-safety tests for the live synthetic acceptance harness."""
from __future__ import annotations

import json
from typing import Any

from cosmaneura_billing import azure_openai_acceptance
from cosmaneura_billing.azure_openai_acceptance import run_acceptance
from cosmaneura_billing.llm import TerminalLLMError
from cosmaneura_billing.log_scanner import scan_log_text
from cosmaneura_billing.runtime import AZURE_OPENAI_ACCEPTANCE_LOG_EVENT
from cosmaneura_billing.settings import Settings


ENDPOINT_CANARY = "https://acceptance-canary.openai.azure.com/"
IDENTITY_CANARY = "11111111-1111-4111-8111-111111111111"
SECRET_CANARY = "synthetic-direct-credential-must-not-appear"


def _settings() -> Settings:
    return Settings(
        azure_openai_enabled=True,
        azure_openai_endpoint=ENDPOINT_CANARY,
        azure_openai_deployment="gpt-5-6-sol-2026-07-09",
        azure_openai_model="gpt-5.6-sol",
        azure_openai_api_version="2025-04-01-preview",
        azure_client_id=IDENTITY_CANARY,
    )


def _private_resolver(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
    del args, kwargs
    return [(2, 1, 6, "", ("10.20.2.39", 443))]


class PassingClient:
    mode = "azure_openai"
    model_label = "gpt-5.6-sol"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 8000,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "schema": schema,
        })
        if max_tokens == 50:
            raise TerminalLLMError(
                "response hit max_output_tokens before the result was complete"
            )
        if schema and schema.get("required") == ["status", "count"]:
            return {"status": "synthetic-ok", "count": 1}
        if schema and schema.get("required") == ["vitals"]:
            return {"vitals": {"pulse": 72, "unit": "bpm"}}
        return {"status": "blocked"}

    def shutdown_metrics(self) -> None:
        self.closed = True


def test_green_harness_is_fixed_synthetic_only_and_scanner_clean() -> None:
    client = PassingClient()
    lines: list[str] = []

    result = run_acceptance(
        _settings(),
        environ={"UNRELATED_SECRET_CANARY": SECRET_CANARY},
        resolver=_private_resolver,
        client_factory=lambda settings: client,
        output=lines.append,
    )

    assert result == 0
    assert len(client.calls) == 4
    assert client.closed is True
    assert all("Synthetic" in call["system"] for call in client.calls)
    rendered = "\n".join(lines)
    events = [json.loads(line) for line in lines]
    assert events[-1] == {
        "event": AZURE_OPENAI_ACCEPTANCE_LOG_EVENT,
        "check": "summary",
        "status": "pass",
        "passed": 8,
        "failed": 0,
        "skipped": 1,
    }
    assert scan_log_text(
        rendered,
        prohibited_values=(ENDPOINT_CANARY, IDENTITY_CANARY, SECRET_CANARY),
    ) == []
    for value in (
        ENDPOINT_CANARY,
        IDENTITY_CANARY,
        SECRET_CANARY,
        "pulse 72",
        "seriously injure",
        "synthetic-ok",
    ):
        assert value not in rendered


def test_client_failure_is_sanitized_and_dependent_checks_skip() -> None:
    canary = "patient-jane-private-note"
    lines: list[str] = []

    def fail_client(settings: Settings) -> Any:
        del settings
        raise RuntimeError(canary)

    assert run_acceptance(
        _settings(),
        environ={},
        resolver=_private_resolver,
        client_factory=fail_client,
        output=lines.append,
    ) == 1

    rendered = "\n".join(lines)
    summary = json.loads(lines[-1])
    assert summary["status"] == "fail"
    assert summary["skipped"] == 5
    assert canary not in rendered
    assert ENDPOINT_CANARY not in rendered
    assert scan_log_text(rendered, prohibited_values=(canary, ENDPOINT_CANARY)) == []


def test_public_dns_and_direct_credentials_fail_without_echoing_values() -> None:
    lines: list[str] = []

    def public_resolver(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        del args, kwargs
        return [(2, 1, 6, "", ("203.0.113.19", 443))]

    assert run_acceptance(
        _settings(),
        environ={"AZURE_OPENAI_API_KEY": SECRET_CANARY},
        resolver=public_resolver,
        client_factory=lambda settings: object(),
        output=lines.append,
    ) == 1

    events = [json.loads(line) for line in lines]
    by_check = {event["check"]: event["status"] for event in events}
    assert by_check["private_dns"] == "fail"
    assert by_check["forbidden_credentials_absent"] == "fail"
    rendered = "\n".join(lines)
    assert SECRET_CANARY not in rendered
    assert "203.0.113.19" not in rendered
    assert scan_log_text(rendered, prohibited_values=(SECRET_CANARY,)) == []


def test_public_dns_blocks_client_construction_even_without_direct_credentials() -> None:
    lines: list[str] = []
    client_calls: list[Settings] = []

    def public_resolver(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        del args, kwargs
        return [(2, 1, 6, "", ("203.0.113.19", 443))]

    def forbidden_client(settings: Settings) -> Any:
        client_calls.append(settings)
        raise AssertionError("client construction must be gated by private DNS")

    assert run_acceptance(
        _settings(),
        environ={},
        resolver=public_resolver,
        client_factory=forbidden_client,
        output=lines.append,
    ) == 1

    assert client_calls == []
    by_check = {
        event["check"]: event["status"]
        for event in (json.loads(line) for line in lines)
    }
    assert by_check["private_dns"] == "fail"
    assert by_check["managed_identity_client_routing"] == "fail"


def test_wrong_client_routing_blocks_all_data_plane_calls() -> None:
    class WrongRoutingClient:
        mode = "mock"
        model_label = "offline-demo"

        def __init__(self) -> None:
            self.complete_calls = 0
            self.closed = False

        def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            self.complete_calls += 1
            raise AssertionError("wrong client must not receive acceptance prompts")

        def shutdown_metrics(self) -> None:
            self.closed = True

    client = WrongRoutingClient()
    lines: list[str] = []

    assert run_acceptance(
        _settings(),
        environ={},
        resolver=_private_resolver,
        client_factory=lambda settings: client,
        output=lines.append,
    ) == 1

    assert client.complete_calls == 0
    assert client.closed is True
    assert json.loads(lines[-1])["skipped"] == 5


def test_scanner_rejects_acceptance_schema_expansion() -> None:
    unsafe = json.dumps({
        "event": AZURE_OPENAI_ACCEPTANCE_LOG_EVENT,
        "check": "private_dns",
        "status": "fail",
        "endpoint": ENDPOINT_CANARY,
    })
    codes = {finding.code for finding in scan_log_text(unsafe)}
    assert "invalid_acceptance_schema" in codes


def test_cli_configuration_failure_never_renders_exception_text(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    canary = "patient-jane-configuration-exception"

    def fail_settings() -> Settings:
        raise RuntimeError(canary)

    monkeypatch.setattr(
        azure_openai_acceptance.Settings,
        "from_env",
        staticmethod(fail_settings),
    )
    monkeypatch.setattr(
        azure_openai_acceptance,
        "configure_json_logging",
        lambda: None,
    )

    assert azure_openai_acceptance.main() == 1
    rendered = capsys.readouterr().out
    assert canary not in rendered
    assert scan_log_text(rendered, prohibited_values=(canary,)) == []
