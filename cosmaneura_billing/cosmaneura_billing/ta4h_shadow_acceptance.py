"""Fixed synthetic deployment acceptance for the isolated TA4H shadow lane.

This harness accepts no chart input. It validates the exact deployment latches,
private DNS, managed-identity production adapter, redirect refusal, one bundled
synthetic TA4H analysis, output non-influence, and the closed aggregate metric
schema. Output is a fixed pass/fail/skip event contract with no endpoint,
identity, response, exception, code, or encounter fields.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urlsplit

from .corpus.demo import SAMPLE_VISIT
from .llm import MockLLM
from .orchestrator import BillingOrchestrator
from .persistence import SYNTHETIC_DEMO_SOURCE
from .runtime import (
    TA4H_SHADOW_ACCEPTANCE_LOG_EVENT,
    configure_json_logging,
    is_safe_ta4h_shadow_acceptance_event,
)
from .settings import Settings
from .ta4h import (
    AzureLanguageTA4H,
    TA4HServiceError,
    _NoRedirectHandler,
    describe_ta4h_client,
    get_default_ta4h_client,
)
from .ta4h_shadow import is_safe_ta4h_shadow_snapshot


EXPECTED_OPENAI_MODEL = "gpt-5.6-sol"
EXPECTED_OPENAI_DEPLOYMENT = "gpt-5-6-sol-2026-07-09"
EXPECTED_OPENAI_API_VERSION = "2025-04-01-preview"
EXPECTED_LANGUAGE_API_VERSION = "2023-04-01"
_FORBIDDEN_CREDENTIALS = (
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_AD_TOKEN",
    "AZURE_LANGUAGE_KEY",
    "AZURE_LANGUAGE_AD_TOKEN",
    "AZURE_AI_LANGUAGE_KEY",
)
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_CHECKS = (
    "configuration",
    "private_dns",
    "forbidden_credentials_absent",
    "managed_identity_client_routing",
    "redirect_policy",
    "synthetic_analysis",
    "no_influence",
    "observability_schema",
)


def _private_dns_passes(
    endpoint: str,
    resolver: Callable[..., list[tuple[Any, ...]]],
) -> bool:
    host = urlsplit(endpoint).hostname
    if not host:
        return False
    rows = resolver(host, 443, type=socket.SOCK_STREAM)
    raw_addresses = {
        row[4][0].split("%", 1)[0]
        for row in rows
        if len(row) > 4 and row[4]
    }
    if not raw_addresses:
        return False
    try:
        addresses = [ipaddress.ip_address(value) for value in raw_addresses]
    except ValueError:
        return False
    return all(
        isinstance(address, ipaddress.IPv4Address)
        and any(address in network for network in _PRIVATE_NETWORKS)
        for address in addresses
    )


def _event(check: str, status: str) -> dict[str, Any]:
    payload = {
        "event": TA4H_SHADOW_ACCEPTANCE_LOG_EVENT,
        "check": check,
        "status": status,
    }
    if not is_safe_ta4h_shadow_acceptance_event(payload):
        raise RuntimeError("invalid TA4H shadow acceptance event")
    return payload


def _summary(statuses: list[str]) -> dict[str, Any]:
    payload = {
        "event": TA4H_SHADOW_ACCEPTANCE_LOG_EVENT,
        "check": "summary",
        "status": "fail" if "fail" in statuses else "pass",
        "passed": statuses.count("pass"),
        "failed": statuses.count("fail"),
        "skipped": statuses.count("skip"),
    }
    if not is_safe_ta4h_shadow_acceptance_event(payload):
        raise RuntimeError("invalid TA4H shadow acceptance summary")
    return payload


def _write(payload: Mapping[str, Any], output: Callable[[str], None]) -> None:
    output(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def run_acceptance(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    client_factory: Callable[[Settings], Any] = get_default_ta4h_client,
    output: Callable[[str], None] = print,
) -> int:
    """Run the immutable synthetic shadow checks and return an exit code."""

    env = os.environ if environ is None else environ
    statuses: list[str] = []

    def record(check: str, status: str) -> None:
        statuses.append(status)
        _write(_event(check, status), output)

    configuration_ok = bool(
        settings.azure_openai_enabled
        and settings.azure_openai_synthetic_only
        and settings.azure_openai_endpoint
        and settings.azure_openai_deployment == EXPECTED_OPENAI_DEPLOYMENT
        and settings.azure_openai_model == EXPECTED_OPENAI_MODEL
        and settings.azure_openai_api_version == EXPECTED_OPENAI_API_VERSION
        and settings.azure_language_shadow_enabled
        and not settings.azure_language_enabled
        and settings.azure_language_endpoint
        and settings.azure_language_api_version == EXPECTED_LANGUAGE_API_VERSION
        and settings.azure_client_id
    )
    record("configuration", "pass" if configuration_ok else "fail")

    try:
        private_dns = configuration_ok and _private_dns_passes(
            settings.azure_language_endpoint, resolver,
        )
    except Exception:
        private_dns = False
    record("private_dns", "pass" if private_dns else "fail")

    credentials_absent = not any(
        (env.get(name) or "").strip() for name in _FORBIDDEN_CREDENTIALS
    )
    record(
        "forbidden_credentials_absent",
        "pass" if credentials_absent else "fail",
    )

    client: Any | None = None
    if configuration_ok and private_dns and credentials_absent:
        try:
            client = client_factory(settings)
            routing_ok = (
                isinstance(client, AzureLanguageTA4H)
                and describe_ta4h_client(client) == {
                    "extractionEngines": 2,
                    "secondaryExtractionMode": "azure_language",
                }
            )
        except Exception:
            routing_ok = False
    else:
        routing_ok = False
    record("managed_identity_client_routing", "pass" if routing_ok else "fail")

    redirect_ok = False
    if routing_ok and client is not None:
        try:
            handler_refused = _NoRedirectHandler().redirect_request(
                None, None, 302, "", {}, "https://attacker.invalid/redirect",
            ) is None
            try:
                client._validated_service_url(  # noqa: SLF001 - acceptance invariant
                    "https://attacker.invalid/language/analyze-text/jobs/1"
                )
            except TA4HServiceError:
                cross_origin_refused = True
            else:
                cross_origin_refused = False
            redirect_ok = handler_refused and cross_origin_refused
        except Exception:
            redirect_ok = False
    record("redirect_policy", "pass" if redirect_ok else "fail")

    analysis_ok = False
    influence_ok = False
    observability_ok = False
    shadow: BillingOrchestrator | None = None
    if routing_ok and redirect_ok and client is not None:
        try:
            base_settings = replace(
                settings,
                azure_language_shadow_enabled=False,
                cpt_auto_fetch=False,
            )
            shadow_settings = replace(settings, cpt_auto_fetch=False)
            baseline = BillingOrchestrator(
                settings=base_settings, llm=MockLLM(), ta4h=None,
            ).process(
                SAMPLE_VISIT, input_provenance=SYNTHETIC_DEMO_SOURCE,
            )
            shadow = BillingOrchestrator(
                settings=shadow_settings, llm=MockLLM(), ta4h=client,
            )
            observed = shadow.process(
                SAMPLE_VISIT, input_provenance=SYNTHETIC_DEMO_SOURCE,
            )
            influence_ok = asdict(observed) == asdict(baseline)
            idle = shadow.wait_for_shadow(130.0)
            snapshot = shadow.ta4h_observability()
            analysis_ok = (
                idle
                and snapshot["succeeded"] == 1
                and snapshot["secondaryEntityCount"] > 0
                and snapshot["secondaryDiagnosisCandidates"] > 0
            )
            observability_ok = (
                is_safe_ta4h_shadow_snapshot(snapshot)
                and snapshot["mode"] == "shadow"
                and snapshot["runs"] == 1
                and snapshot["status"] == "healthy"
            )
        except Exception:
            analysis_ok = False
            influence_ok = False
            observability_ok = False
        finally:
            if shadow is not None:
                shadow.shutdown()
    record("synthetic_analysis", "pass" if analysis_ok else "fail")
    record("no_influence", "pass" if influence_ok else "fail")
    record("observability_schema", "pass" if observability_ok else "fail")

    _write(_summary(statuses), output)
    return 1 if "fail" in statuses else 0


def main() -> int:
    configure_json_logging()
    try:
        settings = Settings.from_env()
    except Exception:
        statuses = ["fail", *("skip" for _ in _CHECKS[1:])]
        _write(_event("configuration", "fail"), print)
        for check in _CHECKS[1:]:
            _write(_event(check, "skip"), print)
        _write(_summary(statuses), print)
        return 1
    return run_acceptance(settings)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EXPECTED_LANGUAGE_API_VERSION",
    "EXPECTED_OPENAI_API_VERSION",
    "EXPECTED_OPENAI_DEPLOYMENT",
    "EXPECTED_OPENAI_MODEL",
    "main",
    "run_acceptance",
]
