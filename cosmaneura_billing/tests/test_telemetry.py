"""Closed, API-only Azure Monitor LLM metric export."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import cosmaneura_billing.telemetry as telemetry
from cosmaneura_billing.telemetry import (
    AzureMonitorFactories,
    AzureMonitorLLMMetricsSink,
    CACHE_CREATION_TOKEN_METRIC,
    CACHE_READ_TOKEN_METRIC,
    INPUT_TOKEN_METRIC,
    LATENCY_METRIC,
    METER_NAME,
    OUTPUT_TOKEN_METRIC,
    REQUEST_COUNT_METRIC,
    SERVICE_NAME,
    create_azure_monitor_llm_sink,
)


CLIENT_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_STRING = (
    "InstrumentationKey=22222222-2222-4222-8222-222222222222;"
    "IngestionEndpoint=https://example.applicationinsights.azure.com/"
)


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
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
    event.update(overrides)
    return event


@dataclass
class FakeInstrument:
    calls: list[tuple[str, int, dict[str, str | int]]] = field(
        default_factory=list,
    )
    fail: bool = False

    def add(self, value: int, attributes: dict[str, str | int]) -> None:
        if self.fail:
            raise RuntimeError("synthetic exporter failure with patient data")
        self.calls.append(("add", value, dict(attributes)))

    def record(self, value: int, attributes: dict[str, str | int]) -> None:
        if self.fail:
            raise RuntimeError("synthetic exporter failure with patient data")
        self.calls.append(("record", value, dict(attributes)))


class FakeMeter:
    def __init__(self) -> None:
        self.instruments: dict[str, FakeInstrument] = {}
        self.definitions: dict[str, dict[str, str]] = {}

    def create_counter(self, name: str, **kwargs: str) -> FakeInstrument:
        instrument = FakeInstrument()
        self.instruments[name] = instrument
        self.definitions[name] = kwargs
        return instrument

    def create_histogram(self, name: str, **kwargs: str) -> FakeInstrument:
        instrument = FakeInstrument()
        self.instruments[name] = instrument
        self.definitions[name] = kwargs
        return instrument


def test_sink_exports_only_numeric_metrics_with_four_bounded_dimensions():
    meter = FakeMeter()
    sink = AzureMonitorLLMMetricsSink(meter)

    sink(_event())

    attributes = {
        "provider": "azure_openai",
        "model": "gpt-4.1-mini-2025-04-14",
        "status": "success",
        "attempt": 2,
    }
    assert meter.instruments[REQUEST_COUNT_METRIC].calls == [
        ("add", 1, attributes),
    ]
    assert meter.instruments[LATENCY_METRIC].calls == [
        ("record", 125, attributes),
    ]
    assert meter.instruments[INPUT_TOKEN_METRIC].calls == [
        ("add", 21, attributes),
    ]
    assert meter.instruments[OUTPUT_TOKEN_METRIC].calls == [
        ("add", 3, attributes),
    ]
    assert meter.instruments[CACHE_CREATION_TOKEN_METRIC].calls == [
        ("add", 8, attributes),
    ]
    assert meter.instruments[CACHE_READ_TOKEN_METRIC].calls == [
        ("add", 13, attributes),
    ]
    assert meter.definitions[REQUEST_COUNT_METRIC]["unit"] == "{request}"
    assert meter.definitions[LATENCY_METRIC]["unit"] == "ms"


def test_missing_provider_usage_emits_attempt_count_without_inventing_values():
    meter = FakeMeter()
    sink = AzureMonitorLLMMetricsSink(meter)

    sink(_event(
        latency_ms=None,
        input_tokens=None,
        output_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    ))

    assert len(meter.instruments[REQUEST_COUNT_METRIC].calls) == 1
    for name in (
        LATENCY_METRIC,
        INPUT_TOKEN_METRIC,
        OUTPUT_TOKEN_METRIC,
        CACHE_CREATION_TOKEN_METRIC,
        CACHE_READ_TOKEN_METRIC,
    ):
        assert meter.instruments[name].calls == []


@pytest.mark.parametrize(
    "unsafe_event",
    [
        _event(prompt="Jane Patient MRN 123"),
        _event(model="gpt-4.1-mini-jane-patient"),
        _event(status="Jane Patient failed"),
        _event(attempt=0),
        _event(input_tokens=-1),
        _event(request_id="11111111111141118111111111111111"),
    ],
)
def test_invalid_or_expanded_event_is_discarded_in_full(unsafe_event):
    meter = FakeMeter()
    sink = AzureMonitorLLMMetricsSink(meter)

    sink(unsafe_event)

    assert all(not instrument.calls for instrument in meter.instruments.values())


def test_instrument_failure_never_escapes_the_sink():
    meter = FakeMeter()
    sink = AzureMonitorLLMMetricsSink(meter)
    meter.instruments[REQUEST_COUNT_METRIC].fail = True

    sink(_event())

    assert all(not instrument.calls for instrument in meter.instruments.values())


def test_hostile_event_mapping_never_escapes_the_sink():
    meter = FakeMeter()
    sink = AzureMonitorLLMMetricsSink(meter)

    class HostileEvent(dict):
        def get(self, key, default=None):
            raise RuntimeError("patient data in hostile mapping")

    sink(HostileEvent(_event()))

    assert all(not instrument.calls for instrument in meter.instruments.values())


class FakeProvider:
    def __init__(self, meter: FakeMeter, calls: list[tuple[str, Any]]) -> None:
        self.meter = meter
        self.calls = calls
        self.shutdown_calls = 0

    def get_meter(self, name: str) -> FakeMeter:
        self.calls.append(("get_meter", name))
        return self.meter

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _factories(
    calls: list[tuple[str, Any]], meter: FakeMeter, providers: list[FakeProvider],
) -> AzureMonitorFactories:
    def credential(**kwargs):
        calls.append(("credential", kwargs))
        return "credential-object"

    def exporter(**kwargs):
        calls.append(("exporter", kwargs))
        return "exporter-object"

    def reader(exporter_object, **kwargs):
        calls.append(("reader", (exporter_object, kwargs)))
        return "reader-object"

    def resource_create(attributes):
        calls.append(("resource", attributes))
        return "resource-object"

    def provider(**kwargs):
        calls.append(("provider", kwargs))
        result = FakeProvider(meter, calls)
        providers.append(result)
        return result

    return AzureMonitorFactories(
        managed_identity_credential=credential,
        metric_exporter=exporter,
        periodic_reader=reader,
        resource_create=resource_create,
        meter_provider=provider,
    )


def test_factory_uses_uami_aad_exporter_and_private_meter_provider():
    calls: list[tuple[str, Any]] = []
    providers: list[FakeProvider] = []
    meter = FakeMeter()

    sink = create_azure_monitor_llm_sink(
        enabled=True,
        connection_string=CONNECTION_STRING,
        managed_identity_client_id=CLIENT_ID,
        factories=_factories(calls, meter, providers),
    )

    assert isinstance(sink, AzureMonitorLLMMetricsSink)
    assert calls == [
        ("credential", {"client_id": CLIENT_ID}),
        (
            "exporter",
            {
                "connection_string": CONNECTION_STRING,
                "credential": "credential-object",
                "disable_offline_storage": True,
            },
        ),
        (
            "reader",
            ("exporter-object", {"export_interval_millis": 60_000}),
        ),
        ("resource", {"service.name": SERVICE_NAME}),
        (
            "provider",
            {
                "metric_readers": ["reader-object"],
                "resource": "resource-object",
            },
        ),
        ("get_meter", METER_NAME),
    ]
    sink(_event())
    assert meter.instruments[REQUEST_COUNT_METRIC].calls
    sink.shutdown()
    assert providers[0].shutdown_calls == 1


@pytest.mark.parametrize(
    ("enabled", "connection_string", "client_id"),
    [
        (False, CONNECTION_STRING, CLIENT_ID),
        (True, "", CLIENT_ID),
        (True, "line\nbreak", CLIENT_ID),
        (True, CONNECTION_STRING, "not-a-managed-identity-uuid"),
        (True, CONNECTION_STRING, "{11111111-1111-4111-8111-111111111111}"),
    ],
)
def test_disabled_or_unsafe_configuration_does_not_load_sdks(
    monkeypatch, enabled, connection_string, client_id,
):
    def must_not_load():
        raise AssertionError("optional SDKs must not load")

    monkeypatch.setattr(telemetry, "_load_default_factories", must_not_load)

    assert create_azure_monitor_llm_sink(
        enabled=enabled,
        connection_string=connection_string,
        managed_identity_client_id=client_id,
    ) is None


def test_missing_optional_sdk_or_constructor_failure_disables_sink(monkeypatch):
    def missing_sdk():
        raise ImportError("dependency details must not escape")

    monkeypatch.setattr(telemetry, "_load_default_factories", missing_sdk)

    assert create_azure_monitor_llm_sink(
        enabled=True,
        connection_string=CONNECTION_STRING,
        managed_identity_client_id=CLIENT_ID,
    ) is None


def test_default_factory_disables_all_auxiliary_azure_monitor_collection(
    monkeypatch,
):
    pytest.importorskip("azure.monitor.opentelemetry.exporter")
    names = (
        "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL",
        "APPLICATIONINSIGHTS_SDKSTATS_DISABLED",
        "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    factories = telemetry._load_default_factories()

    assert isinstance(factories, AzureMonitorFactories)
    for name in names:
        assert telemetry.os.environ[name] == "true"


def test_partial_factory_failure_shuts_down_provider_without_raising():
    calls: list[tuple[str, Any]] = []
    providers: list[FakeProvider] = []

    class BrokenMeter(FakeMeter):
        def create_counter(self, name: str, **kwargs: str) -> FakeInstrument:
            raise RuntimeError("instrument creation failed")

    sink = create_azure_monitor_llm_sink(
        enabled=True,
        connection_string=CONNECTION_STRING,
        managed_identity_client_id=CLIENT_ID,
        factories=_factories(calls, BrokenMeter(), providers),
    )

    assert sink is None
    assert providers[0].shutdown_calls == 1


def test_provider_construction_failure_stops_periodic_reader_thread():
    meter = FakeMeter()

    class FakeCredential:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class FakeReader:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    credential = FakeCredential()
    reader = FakeReader()

    factories = AzureMonitorFactories(
        managed_identity_credential=lambda **_kwargs: credential,
        metric_exporter=lambda **_kwargs: object(),
        periodic_reader=lambda *_args, **_kwargs: reader,
        resource_create=lambda _attributes: object(),
        meter_provider=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider construction failed with endpoint details")
        ),
    )

    sink = create_azure_monitor_llm_sink(
        enabled=True,
        connection_string=CONNECTION_STRING,
        managed_identity_client_id=CLIENT_ID,
        factories=factories,
    )

    assert sink is None
    assert reader.shutdown_calls == 1
    assert credential.close_calls == 1


def test_shutdown_failure_is_nonfatal():
    meter = FakeMeter()

    class BrokenProvider:
        def shutdown(self):
            raise RuntimeError("shutdown failed with endpoint details")

    sink = AzureMonitorLLMMetricsSink(meter, provider=BrokenProvider())
    sink.shutdown()
