"""AzureOpenAILLM hardening: managed identity, structured output, and retries.

All provider calls are faked; tests never acquire a token or use a network.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

import cosmaneura_billing.llm as llm_module
from cosmaneura_billing.llm import (
    AzureOpenAILLM,
    TerminalLLMError,
    _strict_structured_schema,
    validate_json,
)
from cosmaneura_billing.settings import SettingsError


ENDPOINT = "https://cab-openai.openai.azure.com"
DEPLOYMENT = "cab-gpt"
MODEL = "gpt-4.1-mini"
SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = FakeResponses(responses)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("provider error with private response body")
        self.status_code = status_code


def _response(
    result: dict[str, Any] | str = '{"answer":"ok"}',
    *,
    status: str | None = "completed",
    output: list[Any] | None = None,
    usage: dict[str, Any] | None = None,
    incomplete_reason: str | None = None,
    error: Any = None,
    model: str | None = None,
) -> SimpleNamespace:
    text = json.dumps(result) if isinstance(result, dict) else result
    usage_value = None
    if usage is not None:
        details = usage.get("input_tokens_details")
        usage_value = SimpleNamespace(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            input_tokens_details=(
                SimpleNamespace(**details) if isinstance(details, dict) else None
            ),
        )
    return SimpleNamespace(
        status=status,
        output_text=text,
        output=output or [],
        usage=usage_value,
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
        ),
        error=error,
        model=model,
    )


def _client(*responses: Any, **kwargs: Any) -> tuple[AzureOpenAILLM, FakeClient]:
    fake = FakeClient(list(responses))
    adapter = AzureOpenAILLM(
        endpoint=ENDPOINT,
        deployment=DEPLOYMENT,
        model=kwargs.pop("model", MODEL),
        client=fake,
        **kwargs,
    )
    return adapter, fake


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    return sleeps


# ---- local validation ----------------------------------------------------- #
def test_validate_accepts_valid() -> None:
    validate_json({"answer": "yes"}, SCHEMA)


def test_validate_rejects_missing_required() -> None:
    with pytest.raises(ValueError, match="missing required key 'answer'"):
        validate_json({}, SCHEMA)


def test_validate_rejects_wrong_type_with_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
        },
    }
    with pytest.raises(ValueError, match=r"\$\.items\[1\]"):
        validate_json({"items": [1, "two"]}, schema)


def test_validate_union_types() -> None:
    schema = {
        "type": "object",
        "properties": {"t": {"type": ["integer", "null"]}},
    }
    validate_json({"t": None}, schema)
    validate_json({"t": 5}, schema)
    with pytest.raises(ValueError):
        validate_json({"t": "5"}, schema)


# ---- Responses API and Structured Outputs -------------------------------- #
def test_compatible_schema_uses_strict_structured_outputs(no_sleep: list[float]) -> None:
    adapter, fake = _client(_response({"answer": "42"}))

    assert adapter.complete_json("sys", "user", schema=SCHEMA) == {"answer": "42"}

    request = fake.responses.calls[0]
    assert request["model"] == DEPLOYMENT
    assert request["max_output_tokens"] == 8000
    assert request["instructions"] == "sys"
    assert request["input"] == "user"
    assert request["store"] is False
    assert request["text"]["format"] == {
        "type": "json_schema",
        "name": "billing_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    }


def test_optional_fields_become_nullable_then_null_placeholders_are_removed(
    no_sleep: list[float],
) -> None:
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {
            "answer": {"type": "string"},
            "explanation": {"type": "string"},
        },
    }
    adapter, fake = _client(_response({"answer": "ok", "explanation": None}))

    assert adapter.complete_json("s", "u", schema=schema) == {"answer": "ok"}
    strict = fake.responses.calls[0]["text"]["format"]["schema"]
    assert strict["required"] == ["answer", "explanation"]
    assert strict["properties"]["explanation"]["type"] == ["string", "null"]


def test_unconstrained_object_uses_json_mode_without_narrowing_schema(
    no_sleep: list[float],
) -> None:
    schema = {
        "type": "object",
        "required": ["vitals"],
        "properties": {"vitals": {"type": "object"}},
    }
    adapter, fake = _client(_response({"vitals": {"bmi": 37.6}}))

    assert _strict_structured_schema(schema) is None
    assert adapter.complete_json("s", "u", schema=schema) == {
        "vitals": {"bmi": 37.6},
    }
    request = fake.responses.calls[0]
    assert request["text"] == {"format": {"type": "json_object"}}
    assert request["input"] == "u\n\nReturn a single valid JSON object."
    assert "valid JSON object" in request["instructions"]
    assert "JSON Schema contract" in request["instructions"]
    assert '"required":["vitals"]' in request["instructions"]
    assert '"vitals":{"type":"object"}' in request["instructions"]


def test_no_schema_uses_json_mode(no_sleep: list[float]) -> None:
    adapter, fake = _client(_response({"answer": "ok"}))
    assert adapter.complete_json("sys", "user") == {"answer": "ok"}
    assert fake.responses.calls[0]["text"] == {
        "format": {"type": "json_object"},
    }
    assert fake.responses.calls[0]["input"] == (
        "user\n\nReturn a single valid JSON object."
    )
    assert "JSON Schema contract" not in fake.responses.calls[0]["instructions"]
    assert fake.responses.calls[0]["store"] is False


def test_real_sdk_serializes_managed_identity_responses_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import openai

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_test",
                "created_at": 0,
                "model": MODEL,
                "object": "response",
                "output": [{
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text",
                        "text": '{"answer":"ok"}',
                        "annotations": [],
                    }],
                }],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "status": "completed",
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 1,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 2,
                },
            },
        )

    sdk_client = openai.AzureOpenAI(
        azure_endpoint=ENDPOINT,
        api_version="2025-04-01-preview",
        azure_ad_token_provider=lambda: "synthetic-mi-token",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    metrics: list[dict[str, Any]] = []
    adapter = AzureOpenAILLM(
        endpoint=ENDPOINT,
        deployment=DEPLOYMENT,
        model=MODEL,
        client=sdk_client,
        metrics_sink=metrics.append,
    )
    try:
        assert adapter.complete_json("system", "user", schema=SCHEMA) == {
            "answer": "ok",
        }
    finally:
        adapter.shutdown_metrics()

    assert seen["url"] == (
        "https://cab-openai.openai.azure.com/openai/responses"
        "?api-version=2025-04-01-preview"
    )
    assert seen["authorization"] == "Bearer synthetic-mi-token"
    assert seen["body"]["model"] == DEPLOYMENT
    assert seen["body"]["store"] is False
    assert seen["body"]["text"]["format"]["type"] == "json_schema"
    assert metrics[0]["input_tokens"] == 1
    assert metrics[0]["output_tokens"] == 1
    assert metrics[0]["cache_read_input_tokens"] == 0


# ---- retry and terminal behavior ----------------------------------------- #
def test_retries_on_429_then_succeeds(no_sleep: list[float]) -> None:
    adapter, _ = _client(StatusError(429), _response(), max_retries=2)
    assert adapter.complete_json("s", "u", schema=SCHEMA) == {"answer": "ok"}
    assert no_sleep == [2]


def test_retries_on_off_schema_output(no_sleep: list[float]) -> None:
    adapter, _ = _client(
        _response({"wrong": 1}),
        _response({"answer": "ok"}),
        max_retries=2,
    )
    assert adapter.complete_json("s", "u", schema=SCHEMA) == {"answer": "ok"}


def test_retries_on_network_timeout(no_sleep: list[float]) -> None:
    adapter, _ = _client(TimeoutError(), _response(), max_retries=2)
    assert adapter.complete_json("s", "u", schema=SCHEMA) == {"answer": "ok"}


def test_non_retryable_http_error_raises_immediately(no_sleep: list[float]) -> None:
    adapter, _ = _client(StatusError(401), max_retries=3)
    with pytest.raises(StatusError):
        adapter.complete_json("s", "u", schema=SCHEMA)
    assert no_sleep == []


def test_structured_output_rejection_never_falls_back_to_weaker_format(
    no_sleep: list[float],
) -> None:
    adapter, fake = _client(StatusError(400), max_retries=3)
    with pytest.raises(StatusError):
        adapter.complete_json("s", "u", schema=SCHEMA)
    assert len(fake.responses.calls) == 1
    assert fake.responses.calls[0]["text"]["format"]["type"] == "json_schema"
    assert no_sleep == []


def test_json_mode_rejection_is_terminal_without_changing_format(
    no_sleep: list[float],
) -> None:
    schema = {
        "type": "object",
        "required": ["vitals"],
        "properties": {"vitals": {"type": "object"}},
    }
    adapter, fake = _client(StatusError(400), max_retries=3)

    with pytest.raises(StatusError):
        adapter.complete_json("s", "u", schema=schema)

    assert len(fake.responses.calls) == 1
    request = fake.responses.calls[0]
    assert request["text"]["format"]["type"] == "json_object"
    assert request["input"] == "u\n\nReturn a single valid JSON object."
    assert no_sleep == []


def test_exhausted_retries_raise_runtime_error(no_sleep: list[float]) -> None:
    adapter, _ = _client(
        StatusError(503), StatusError(503), StatusError(503), max_retries=2,
    )
    with pytest.raises(RuntimeError, match="no valid response after 3 attempts"):
        adapter.complete_json("s", "u", schema=SCHEMA)


def test_refusal_is_terminal_and_does_not_echo_provider_text(
    no_sleep: list[float],
) -> None:
    refusal = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="refusal", refusal="patient Jane private note")],
    )
    adapter, _ = _client(_response(output=[refusal]), max_retries=3)
    with pytest.raises(TerminalLLMError, match="declined") as exc_info:
        adapter.complete_json("s", "u", schema=SCHEMA)
    assert "Jane" not in str(exc_info.value)
    assert no_sleep == []


def test_output_limit_is_terminal_and_diagnosed(no_sleep: list[float]) -> None:
    adapter, _ = _client(
        _response(status="incomplete", incomplete_reason="max_output_tokens"),
        max_retries=3,
    )
    with pytest.raises(TerminalLLMError, match="max_output_tokens"):
        adapter.complete_json("s", "u", schema=SCHEMA)
    assert no_sleep == []


@pytest.mark.parametrize("status", ["queued", "in_progress", "cancelled", None])
def test_only_completed_responses_can_enter_the_coding_pipeline(
    no_sleep: list[float],
    status: str | None,
) -> None:
    adapter, _ = _client(
        _response({"answer": "partial output must not be accepted"}, status=status),
        max_retries=3,
    )
    with pytest.raises(TerminalLLMError, match="unusable response status"):
        adapter.complete_json("s", "u", schema=SCHEMA)
    assert no_sleep == []


# ---- managed identity construction -------------------------------------- #
def test_sdk_client_uses_managed_identity_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import azure.identity
    import openai

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    observed: dict[str, Any] = {}
    credential = object()
    token_provider = object()
    sdk_client = object()

    def credential_factory(**kwargs: Any) -> Any:
        observed["credential"] = kwargs
        return credential

    monkeypatch.setattr(
        azure.identity,
        "ManagedIdentityCredential",
        credential_factory,
    )

    def bearer_provider(value: Any, scope: str) -> Any:
        observed["provider"] = (value, scope)
        return token_provider

    def client_factory(**kwargs: Any) -> Any:
        observed["client"] = kwargs
        return sdk_client

    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", bearer_provider)
    monkeypatch.setattr(openai, "AzureOpenAI", client_factory)

    client, built_credential, _ = AzureOpenAILLM._build_client(
        endpoint=ENDPOINT,
        api_version="2025-04-01-preview",
        managed_identity_client_id="11111111-1111-4111-8111-111111111111",
        timeout=120,
    )

    assert client is sdk_client
    assert built_credential is credential
    assert observed["credential"] == {
        "client_id": "11111111-1111-4111-8111-111111111111",
    }
    assert observed["provider"] == (
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    assert observed["client"]["azure_ad_token_provider"] is token_provider
    assert observed["client"]["max_retries"] == 0
    assert "api_key" not in observed["client"]


def test_real_sdk_client_constructs_with_managed_identity_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    client, credential, _ = AzureOpenAILLM._build_client(
        endpoint=ENDPOINT,
        api_version="2025-04-01-preview",
        managed_identity_client_id="11111111-1111-4111-8111-111111111111",
        timeout=120,
    )
    try:
        assert isinstance(client, openai.AzureOpenAI)
    finally:
        client.close()
        credential.close()


@pytest.mark.parametrize(
    "credential_name",
    ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN"],
)
def test_sdk_construction_rejects_ambient_direct_credentials(
    monkeypatch: pytest.MonkeyPatch,
    credential_name: str,
) -> None:
    secret = "direct-credential-must-not-leak"
    monkeypatch.setenv(credential_name, secret)

    with pytest.raises(SettingsError, match="managed identity") as exc_info:
        AzureOpenAILLM._build_client(
            endpoint=ENDPOINT,
            api_version="2025-04-01-preview",
            managed_identity_client_id="11111111-1111-4111-8111-111111111111",
            timeout=120,
        )

    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "adapter_overrides,config_overrides,unsafe",
    [
        (
            {"endpoint": "https://patient-jane.example.invalid"},
            {},
            "patient-jane",
        ),
        ({"deployment": "patient Jane"}, {}, "patient Jane"),
        ({"model": "gpt-jane-patient"}, {}, "gpt-jane-patient"),
        ({"api_version": "latest"}, {}, "latest"),
        ({}, {"azure_client_id": "patient-jane"}, "patient-jane"),
    ],
)
def test_real_client_validates_effective_routing_and_identity_before_build(
    monkeypatch: pytest.MonkeyPatch,
    adapter_overrides: dict[str, str],
    config_overrides: dict[str, str],
    unsafe: str,
) -> None:
    build_calls: list[dict[str, Any]] = []

    def forbidden_build(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        raise AssertionError("SDK construction must not run")

    monkeypatch.setattr(
        AzureOpenAILLM,
        "_build_client",
        staticmethod(forbidden_build),
    )
    with pytest.raises(SettingsError) as exc_info:
        AzureOpenAILLM(
            endpoint=adapter_overrides.get("endpoint", ENDPOINT),
            deployment=adapter_overrides.get("deployment", DEPLOYMENT),
            model=adapter_overrides.get("model", MODEL),
            api_version=adapter_overrides.get(
                "api_version", "2025-04-01-preview",
            ),
            settings=_config(**config_overrides),
        )

    assert build_calls == []
    assert unsafe not in str(exc_info.value)


# ---- PHI-safe metrics ----------------------------------------------------- #
def test_records_usage_latency_and_cached_tokens_for_each_request(
    no_sleep: list[float],
) -> None:
    first = _response(
        {"wrong": 1},
        usage={
            "input_tokens": 21,
            "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 8},
        },
    )
    second = _response(
        {"answer": "ok"},
        usage={"input_tokens": 22, "output_tokens": 4},
    )
    times = iter([10.0, 10.125, 20.0, 20.250])
    metrics: list[dict[str, Any]] = []
    adapter, _ = _client(
        first,
        second,
        model="gpt-4.1-mini-2025-04-14",
        max_retries=1,
        clock=lambda: next(times),
        metrics_sink=metrics.append,
    )

    assert adapter.complete_json(
        "system contains Jane Doe", "chart contains MRN 123", schema=SCHEMA,
    ) == {"answer": "ok"}
    assert metrics == [
        {
            "event": "llm_request",
            "provider": "azure_openai",
            "model": "gpt-4.1-mini-2025-04-14",
            "status": "invalid_response",
            "attempt": 1,
            "latency_ms": 125,
            "input_tokens": 21,
            "output_tokens": 3,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": 8,
        },
        {
            "event": "llm_request",
            "provider": "azure_openai",
            "model": "gpt-4.1-mini-2025-04-14",
            "status": "success",
            "attempt": 2,
            "latency_ms": 250,
            "input_tokens": 22,
            "output_tokens": 4,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        },
    ]
    assert "Jane Doe" not in json.dumps(metrics)
    assert "MRN 123" not in json.dumps(metrics)


def test_default_metric_log_contains_only_safe_fields(
    no_sleep: list[float], caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _ = _client(
        _response(
            {"answer": "patient response must not be logged"},
            usage={"input_tokens": 8, "output_tokens": 2},
        )
    )

    with caplog.at_level(logging.INFO, logger="cosmaneura_billing.llm"):
        out = adapter.complete_json(
            "patient Alice system prompt", "DOB 2000-01-02", schema=SCHEMA,
        )

    assert out == {"answer": "patient response must not be logged"}
    records = [r for r in caplog.records if r.getMessage() == "llm_request"]
    assert len(records) == 1
    record = records[0]
    assert record.provider == "azure_openai"
    assert record.model == MODEL
    assert record.status == "success"
    rendered = caplog.text + repr(record.__dict__)
    for secret in (
        "Alice", "DOB 2000-01-02", "patient response must not be logged",
    ):
        assert secret not in rendered


def test_terminal_response_records_usage_and_fails_closed(no_sleep: list[float]) -> None:
    refusal = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="refusal", refusal="private text")],
    )
    metrics: list[dict[str, Any]] = []
    adapter, _ = _client(
        _response(
            output=[refusal],
            usage={"input_tokens": 13, "output_tokens": 1},
        ),
        metrics_sink=metrics.append,
    )
    with pytest.raises(TerminalLLMError):
        adapter.complete_json("system", "user", schema=SCHEMA)
    assert metrics[0]["status"] == "terminal_response"
    assert metrics[0]["input_tokens"] == 13
    assert metrics[0]["output_tokens"] == 1


def test_metrics_prefer_safe_provider_model_over_configured_label(
    no_sleep: list[float],
) -> None:
    metrics: list[dict[str, Any]] = []
    adapter, _ = _client(
        _response({"answer": "ok"}, model="gpt-5.6-sol"),
        model="gpt-4.1-mini",
        metrics_sink=metrics.append,
    )
    assert adapter.complete_json("s", "u", schema=SCHEMA) == {"answer": "ok"}
    assert metrics[0]["model"] == "gpt-5.6-sol"


def test_invalid_usage_model_and_clock_values_are_redacted(
    no_sleep: list[float],
) -> None:
    metrics: list[dict[str, Any]] = []
    times = iter([float("nan"), float("inf")])
    adapter, _ = _client(
        _response(
            {"answer": "ok"},
            usage={
                "input_tokens": "patient name",
                "output_tokens": -1,
                "input_tokens_details": {"cached_tokens": True},
            },
        ),
        model="gpt-jane-patient",
        clock=lambda: next(times),
        metrics_sink=metrics.append,
    )
    assert adapter.complete_json("s", "u", schema=SCHEMA) == {"answer": "ok"}
    assert metrics[0]["model"] == "invalid_model_label"
    assert metrics[0]["latency_ms"] is None
    assert metrics[0]["input_tokens"] is None
    assert metrics[0]["output_tokens"] is None
    assert metrics[0]["cache_read_input_tokens"] is None
    assert "jane" not in json.dumps(metrics)


def test_metrics_failure_never_changes_inference_result(no_sleep: list[float]) -> None:
    def broken_sink(_metric: dict[str, Any]) -> None:
        raise RuntimeError("telemetry unavailable")

    adapter, _ = _client(_response(), metrics_sink=broken_sink)
    assert adapter.complete_json("s", "u", schema=SCHEMA) == {"answer": "ok"}


# ---- composition ---------------------------------------------------------- #
def _config(**overrides: Any) -> Any:
    values = {
        "azure_openai_enabled": True,
        "azure_openai_endpoint": ENDPOINT,
        "azure_openai_deployment": DEPLOYMENT,
        "azure_openai_model": MODEL,
        "azure_openai_api_version": "2025-04-01-preview",
        "telemetry_enabled": True,
        "application_insights_connection_string": "synthetic-routing-metadata",
        "azure_client_id": "11111111-1111-4111-8111-111111111111",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_client_wires_and_flushes_explicit_metric_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_calls: list[str] = []

    class Sink:
        def __call__(self, _event: dict[str, Any]) -> None:
            pass

        def shutdown(self) -> None:
            shutdown_calls.append("shutdown")

    sink = Sink()
    observed: dict[str, Any] = {}
    fake_client = FakeClient([])

    def create_sink(**kwargs: Any) -> Sink:
        observed.update(kwargs)
        return sink

    monkeypatch.setattr(llm_module, "create_azure_monitor_llm_sink", create_sink)
    monkeypatch.setattr(
        AzureOpenAILLM,
        "_build_client",
        staticmethod(lambda **_kwargs: (fake_client, None, ())),
    )

    client = llm_module.get_default_client(_config())

    assert isinstance(client, AzureOpenAILLM)
    assert observed == {
        "enabled": True,
        "connection_string": "synthetic-routing-metadata",
        "managed_identity_client_id": "11111111-1111-4111-8111-111111111111",
    }
    client.shutdown_metrics()
    assert shutdown_calls == ["shutdown"]
    assert fake_client.closed is True


def test_default_client_fails_when_enabled_metrics_cannot_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_module, "create_azure_monitor_llm_sink", lambda **_kwargs: None,
    )
    with pytest.raises(RuntimeError, match="metrics exporter is unavailable"):
        llm_module.get_default_client(_config())


def test_default_client_stays_offline_when_flag_is_false_despite_metadata() -> None:
    assert isinstance(
        llm_module.get_default_client(_config(azure_openai_enabled=False)),
        llm_module.MockLLM,
    )
