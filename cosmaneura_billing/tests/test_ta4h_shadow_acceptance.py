"""Contract tests for the fixed synthetic TA4H shadow acceptance harness."""
from __future__ import annotations

import json
from typing import Any

from cosmaneura_billing import ta4h_shadow_acceptance
from cosmaneura_billing.log_scanner import scan_log_text
from cosmaneura_billing.runtime import TA4H_SHADOW_ACCEPTANCE_LOG_EVENT
from cosmaneura_billing.settings import Settings
from cosmaneura_billing.ta4h import AzureLanguageTA4H, MockTA4H
from cosmaneura_billing.ta4h_shadow_acceptance import run_acceptance


IDENTITY_CANARY = "11111111-1111-4111-8111-111111111111"
OPENAI_ENDPOINT = "https://acceptance-openai.openai.azure.com"
LANGUAGE_ENDPOINT = "https://acceptance-language.cognitiveservices.azure.com"
SECRET_CANARY = "direct-credential-secret-canary"


def _settings() -> Settings:
    return Settings(
        environment="test",
        azure_client_id=IDENTITY_CANARY,
        azure_openai_enabled=True,
        azure_openai_endpoint=OPENAI_ENDPOINT,
        azure_openai_deployment="gpt-5-6-sol-2026-07-09",
        azure_openai_model="gpt-5.6-sol",
        azure_openai_api_version="2025-04-01-preview",
        azure_language_shadow_enabled=True,
        azure_language_endpoint=LANGUAGE_ENDPOINT,
        azure_language_api_version="2023-04-01",
        cpt_auto_fetch=False,
    )


def _private_resolver(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
    del args, kwargs
    return [(2, 1, 6, "", ("10.20.2.40", 443))]


class SyntheticAzureLanguage(AzureLanguageTA4H):
    def __init__(self, *, fail: str = "") -> None:
        # Deliberately bypass credentials/network while preserving the exact
        # production adapter type checked by the harness.
        self.endpoint = LANGUAGE_ENDPOINT
        self.api_version = "2023-04-01"
        self.fail = fail

    def analyze(self, documents):
        if self.fail:
            raise RuntimeError(self.fail)
        return MockTA4H().analyze(documents)


def test_green_harness_is_no_influence_and_scanner_clean():
    lines: list[str] = []
    result = run_acceptance(
        _settings(),
        environ={},
        resolver=_private_resolver,
        client_factory=lambda settings: SyntheticAzureLanguage(),
        output=lines.append,
    )

    assert result == 0
    events = [json.loads(line) for line in lines]
    assert events[-1] == {
        "event": TA4H_SHADOW_ACCEPTANCE_LOG_EVENT,
        "check": "summary",
        "status": "pass",
        "passed": 8,
        "failed": 0,
        "skipped": 0,
    }
    rendered = "\n".join(lines)
    assert scan_log_text(
        rendered,
        prohibited_values=(OPENAI_ENDPOINT, LANGUAGE_ENDPOINT, IDENTITY_CANARY),
    ) == []
    for value in (OPENAI_ENDPOINT, LANGUAGE_ENDPOINT, IDENTITY_CANARY, "K62.5"):
        assert value not in rendered


def test_runtime_failure_and_exception_text_are_sanitized():
    canary = "patient Jane Doe MRN-123 private note"
    lines: list[str] = []

    result = run_acceptance(
        _settings(),
        environ={},
        resolver=_private_resolver,
        client_factory=lambda settings: SyntheticAzureLanguage(fail=canary),
        output=lines.append,
    )

    assert result == 1
    rendered = "\n".join(lines)
    assert canary not in rendered
    assert scan_log_text(rendered, prohibited_values=(canary,)) == []
    statuses = {event["check"]: event["status"] for event in map(json.loads, lines)}
    assert statuses["synthetic_analysis"] == "fail"
    # Even a TA4H outage leaves the primary result byte-identical.
    assert statuses["no_influence"] == "pass"


def test_direct_credentials_fail_before_client_construction_without_echo():
    lines: list[str] = []
    constructed = False

    def client_factory(settings):
        nonlocal constructed
        del settings
        constructed = True
        return SyntheticAzureLanguage()

    result = run_acceptance(
        _settings(),
        environ={"AZURE_LANGUAGE_KEY": SECRET_CANARY},
        resolver=_private_resolver,
        client_factory=client_factory,
        output=lines.append,
    )

    assert result == 1
    assert constructed is False
    rendered = "\n".join(lines)
    assert SECRET_CANARY not in rendered
    assert scan_log_text(rendered, prohibited_values=(SECRET_CANARY,)) == []


def test_acceptance_schema_expansion_is_rejected_by_log_scanner():
    unsafe = json.dumps({
        "event": TA4H_SHADOW_ACCEPTANCE_LOG_EVENT,
        "check": "no_influence",
        "status": "pass",
        "patient": "Jane",
    })
    assert "invalid_acceptance_schema" in {
        finding.code for finding in scan_log_text(unsafe)
    }


def test_main_configuration_failure_is_closed_and_sanitized(monkeypatch, capsys):
    canary = "private-config-canary"
    monkeypatch.setattr(
        ta4h_shadow_acceptance.Settings,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError(canary))),
    )
    monkeypatch.setattr(ta4h_shadow_acceptance, "configure_json_logging", lambda: None)

    assert ta4h_shadow_acceptance.main() == 1
    rendered = capsys.readouterr().out
    assert canary not in rendered
    assert scan_log_text(rendered, prohibited_values=(canary,)) == []
