"""Explicit, PHI-free Azure Monitor metrics for LLM provider requests.

This module intentionally does not install Azure/OpenTelemetry automatic
instrumentation.  The API may opt in to this one numeric sink; workers and
other processes get no telemetry exporter merely by importing the package.

Azure dependencies are imported only by :func:`create_azure_monitor_llm_sink`.
They remain optional for the core engine and any configuration or exporter
failure disables the sink without entering the inference decision path.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable
from uuid import UUID

from .runtime import is_safe_llm_event


METER_NAME = "cosmaneura_billing.llm"
SERVICE_NAME = "cosmaneura-billing-api"

REQUEST_COUNT_METRIC = "cosmaneura.llm.requests"
LATENCY_METRIC = "cosmaneura.llm.latency"
INPUT_TOKEN_METRIC = "cosmaneura.llm.input_tokens"
OUTPUT_TOKEN_METRIC = "cosmaneura.llm.output_tokens"
CACHE_CREATION_TOKEN_METRIC = "cosmaneura.llm.cache_creation_input_tokens"
CACHE_READ_TOKEN_METRIC = "cosmaneura.llm.cache_read_input_tokens"

_CONNECTION_STRING_MAX_LENGTH = 4_096
_EXPORT_INTERVAL_MILLIS = 60_000
_AZURE_MONITOR_DISABLE_ENV = (
    "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL",
    "APPLICATIONINSIGHTS_SDKSTATS_DISABLED",
    "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED",
)


@dataclass(frozen=True)
class AzureMonitorFactories:
    """Injectable Azure/OpenTelemetry constructors used by the sink factory.

    Keeping these constructors injectable lets the closed data contract be
    tested without importing optional SDKs or opening a network connection.
    """

    managed_identity_credential: Callable[..., Any]
    metric_exporter: Callable[..., Any]
    periodic_reader: Callable[..., Any]
    resource_create: Callable[[dict[str, str]], Any]
    meter_provider: Callable[..., Any]


def _load_default_factories() -> AzureMonitorFactories:
    """Load the optional runtime SDKs only when the API enables telemetry."""

    # The exporter otherwise starts Statsbeat/customer-SDK metrics and a remote
    # OneSettings control-plane worker.  This deployment admits only the six
    # explicit billing metrics below, so disable all auxiliary collection before
    # importing or constructing the SDK.
    for name in _AZURE_MONITOR_DISABLE_ENV:
        os.environ[name] = "true"

    from azure.identity import ManagedIdentityCredential
    from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    return AzureMonitorFactories(
        managed_identity_credential=ManagedIdentityCredential,
        metric_exporter=AzureMonitorMetricExporter,
        periodic_reader=PeriodicExportingMetricReader,
        resource_create=Resource.create,
        meter_provider=MeterProvider,
    )


def _safe_connection_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _CONNECTION_STRING_MAX_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        return None
    return candidate


def _safe_managed_identity_client_id(value: object) -> str | None:
    """Return a canonical UAMI client UUID or ``None`` for unsafe input."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError):
        return None
    if candidate.lower() != str(parsed):
        return None
    return str(parsed)


class AzureMonitorLLMMetricsSink:
    """Translate the closed LLM event into numeric OpenTelemetry instruments.

    Calls never raise.  An invalid or expanded event is discarded in full, and
    exporter/instrument failures cannot change the associated inference result.
    """

    def __init__(
        self,
        meter: Any,
        *,
        provider: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        # Retain the provider for the lifetime of its readers/exporter.
        self._provider = provider
        self._credential = credential
        self._request_count = meter.create_counter(
            REQUEST_COUNT_METRIC,
            unit="{request}",
            description="Count of Azure OpenAI provider request attempts",
        )
        self._latency = meter.create_histogram(
            LATENCY_METRIC,
            unit="ms",
            description="Azure OpenAI provider request latency",
        )
        self._input_tokens = meter.create_counter(
            INPUT_TOKEN_METRIC,
            unit="{token}",
            description="Azure OpenAI input tokens",
        )
        self._output_tokens = meter.create_counter(
            OUTPUT_TOKEN_METRIC,
            unit="{token}",
            description="Azure OpenAI output tokens",
        )
        self._cache_creation_tokens = meter.create_counter(
            CACHE_CREATION_TOKEN_METRIC,
            unit="{token}",
            description="Azure OpenAI cache-creation input tokens",
        )
        self._cache_read_tokens = meter.create_counter(
            CACHE_READ_TOKEN_METRIC,
            unit="{token}",
            description="Azure OpenAI cache-read input tokens",
        )

    def __call__(self, event: dict[str, Any]) -> None:
        try:
            if not is_safe_llm_event(event):
                return

            # These are the only dimensions admitted to Azure Monitor.  In
            # particular, there is no request, patient, encounter, prompt,
            # response, exception, URL, or other joinable identifier.
            attributes: dict[str, str | int] = {
                "provider": event["provider"],
                "model": event["model"],
                "status": event["status"],
                "attempt": event["attempt"],
            }
            self._request_count.add(1, attributes)
            if event["latency_ms"] is not None:
                self._latency.record(event["latency_ms"], attributes)
            if event["input_tokens"] is not None:
                self._input_tokens.add(event["input_tokens"], attributes)
            if event["output_tokens"] is not None:
                self._output_tokens.add(event["output_tokens"], attributes)
            if event["cache_creation_input_tokens"] is not None:
                self._cache_creation_tokens.add(
                    event["cache_creation_input_tokens"], attributes,
                )
            if event["cache_read_input_tokens"] is not None:
                self._cache_read_tokens.add(
                    event["cache_read_input_tokens"], attributes,
                )
        except Exception:
            # Observability must never become part of the coding decision path.
            return

    def shutdown(self) -> None:
        """Best-effort provider shutdown for an API lifespan hook."""

        for target, method_name in (
            (self._provider, "shutdown"),
            (self._credential, "close"),
        ):
            cleanup = getattr(target, method_name, None)
            if not callable(cleanup):
                continue
            try:
                cleanup()
            except Exception:
                continue


def _best_effort_cleanup(target: Any, method_name: str) -> None:
    cleanup = getattr(target, method_name, None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            pass


def create_azure_monitor_llm_sink(
    *,
    enabled: bool,
    connection_string: str,
    managed_identity_client_id: str,
    factories: AzureMonitorFactories | None = None,
) -> AzureMonitorLLMMetricsSink | None:
    """Build the API's explicit Azure Monitor sink, or safely disable it.

    ``ManagedIdentityCredential`` is always supplied to the Azure Monitor
    exporter so a workspace with local authentication disabled does not fall
    back to an instrumentation-key-only path.  Nothing is registered as the
    global OpenTelemetry provider, and no automatic log/trace/HTTP/SQL
    instrumentation is enabled.
    """

    if enabled is not True:
        return None
    safe_connection_string = _safe_connection_string(connection_string)
    safe_client_id = _safe_managed_identity_client_id(
        managed_identity_client_id,
    )
    if safe_connection_string is None or safe_client_id is None:
        return None

    credential: Any | None = None
    exporter: Any | None = None
    reader: Any | None = None
    provider: Any | None = None
    try:
        constructors = factories or _load_default_factories()
        credential = constructors.managed_identity_credential(
            client_id=safe_client_id,
        )
        exporter = constructors.metric_exporter(
            connection_string=safe_connection_string,
            credential=credential,
            disable_offline_storage=True,
        )
        reader = constructors.periodic_reader(
            exporter,
            export_interval_millis=_EXPORT_INTERVAL_MILLIS,
        )
        resource = constructors.resource_create({"service.name": SERVICE_NAME})
        provider = constructors.meter_provider(
            metric_readers=[reader],
            resource=resource,
        )
        meter = provider.get_meter(METER_NAME)
        return AzureMonitorLLMMetricsSink(
            meter,
            provider=provider,
            credential=credential,
        )
    except Exception:
        # Do not serialize configuration, credential, exporter, or exception
        # details here: SDK errors can contain endpoints or bearer material.
        if provider is not None:
            # Provider shutdown owns its metric readers and exporter.
            _best_effort_cleanup(provider, "shutdown")
        elif reader is not None:
            # Periodic readers start a daemon thread during construction.
            _best_effort_cleanup(reader, "shutdown")
        elif exporter is not None:
            _best_effort_cleanup(exporter, "shutdown")
        if credential is not None:
            _best_effort_cleanup(credential, "close")
        return None


__all__ = [
    "AzureMonitorFactories",
    "AzureMonitorLLMMetricsSink",
    "CACHE_CREATION_TOKEN_METRIC",
    "CACHE_READ_TOKEN_METRIC",
    "INPUT_TOKEN_METRIC",
    "LATENCY_METRIC",
    "METER_NAME",
    "OUTPUT_TOKEN_METRIC",
    "REQUEST_COUNT_METRIC",
    "SERVICE_NAME",
    "create_azure_monitor_llm_sink",
]
