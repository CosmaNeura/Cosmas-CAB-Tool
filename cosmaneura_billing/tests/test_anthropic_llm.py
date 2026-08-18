"""AnthropicLLM hardening: structured output, schema validation, retries.
All HTTP is faked — no network, no key."""
import io
import json
import logging
import urllib.error
import urllib.request

import pytest

import cosmaneura_billing.llm as llm_module
from cosmaneura_billing.llm import AnthropicLLM, TerminalLLMError, validate_json

SCHEMA = {"type": "object", "required": ["answer"],
          "properties": {"answer": {"type": "string"}}}


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tool_payload(inp):
    return {"content": [{"type": "tool_use", "name": "emit_result", "input": inp}]}


def _text_payload(text):
    return {"content": [{"type": "text", "text": text}]}


@pytest.fixture()
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("cosmaneura_billing.llm.time.sleep", sleeps.append)
    return sleeps


def _patch_urlopen(monkeypatch, responses):
    """responses: list of payload dicts or Exceptions, consumed in order."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# ---- validate_json ---------------------------------------------------------- #
def test_validate_accepts_valid():
    validate_json({"answer": "yes"}, SCHEMA)


def test_validate_rejects_missing_required():
    with pytest.raises(ValueError, match="missing required key 'answer'"):
        validate_json({}, SCHEMA)


def test_validate_rejects_wrong_type_with_path():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "integer"}}}}
    with pytest.raises(ValueError, match=r"\$\.items\[1\]"):
        validate_json({"items": [1, "two"]}, schema)


def test_validate_union_types():
    schema = {"type": "object", "properties": {"t": {"type": ["integer", "null"]}}}
    validate_json({"t": None}, schema)
    validate_json({"t": 5}, schema)
    with pytest.raises(ValueError):
        validate_json({"t": "5"}, schema)


# ---- structured output ------------------------------------------------------ #
def test_schema_forces_tool_use_and_returns_input(monkeypatch, no_sleep):
    calls = _patch_urlopen(monkeypatch, [_tool_payload({"answer": "42"})])
    out = AnthropicLLM(api_key="test").complete_json("sys", "user", schema=SCHEMA)
    assert out == {"answer": "42"}
    body = json.loads(calls[0].data)
    assert body["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert body["tools"][0]["input_schema"] == SCHEMA


def test_plain_json_path_strips_fences(monkeypatch, no_sleep):
    _patch_urlopen(monkeypatch, [_text_payload('```json\n{"answer": "ok"}\n```')])
    out = AnthropicLLM(api_key="test").complete_json("sys", "user")
    assert out == {"answer": "ok"}


# ---- retry behaviour --------------------------------------------------------- #
def _http_error(code):
    return urllib.error.HTTPError("https://api", code, "err", {}, io.BytesIO(b""))


def test_retries_on_429_then_succeeds(monkeypatch, no_sleep):
    _patch_urlopen(monkeypatch, [_http_error(429), _tool_payload({"answer": "ok"})])
    out = AnthropicLLM(api_key="test", max_retries=2).complete_json("s", "u", schema=SCHEMA)
    assert out == {"answer": "ok"}
    assert len(no_sleep) == 1  # backed off once


def test_retries_on_off_schema_output(monkeypatch, no_sleep):
    _patch_urlopen(monkeypatch, [_tool_payload({"wrong": 1}),
                                 _tool_payload({"answer": "ok"})])
    out = AnthropicLLM(api_key="test", max_retries=2).complete_json("s", "u", schema=SCHEMA)
    assert out == {"answer": "ok"}


def test_non_retryable_http_error_raises_immediately(monkeypatch, no_sleep):
    _patch_urlopen(monkeypatch, [_http_error(401)])
    with pytest.raises(urllib.error.HTTPError):
        AnthropicLLM(api_key="bad", max_retries=3).complete_json("s", "u", schema=SCHEMA)
    assert no_sleep == []


def test_exhausted_retries_raise_runtime_error(monkeypatch, no_sleep):
    _patch_urlopen(monkeypatch, [_http_error(529)] * 3)
    with pytest.raises(RuntimeError, match="no valid response after 3 attempts"):
        AnthropicLLM(api_key="test", max_retries=2).complete_json("s", "u", schema=SCHEMA)


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    assert AnthropicLLM(api_key="k").model == "claude-sonnet-5"


# ---- PHI-safe usage and latency metrics ------------------------------------ #
def test_records_structured_usage_and_latency_for_each_provider_request(
    monkeypatch, no_sleep,
):
    first = _tool_payload({"wrong": 1})
    first["usage"] = {"input_tokens": 21, "output_tokens": 3}
    second = _tool_payload({"answer": "ok"})
    second["usage"] = {"input_tokens": 22, "output_tokens": 4}
    _patch_urlopen(monkeypatch, [first, second])
    times = iter([10.0, 10.125, 20.0, 20.250])
    metrics = []

    out = AnthropicLLM(
        model="claude-opus-5-20260801",
        api_key="test",
        max_retries=1,
        clock=lambda: next(times),
        metrics_sink=metrics.append,
    ).complete_json("system contains Jane Doe", "chart contains MRN 123", schema=SCHEMA)

    assert out == {"answer": "ok"}
    assert metrics == [
        {
            "event": "llm_request",
            "provider": "anthropic",
            "model": "claude-opus-5-20260801",
            "status": "invalid_response",
            "attempt": 1,
            "latency_ms": 125,
            "input_tokens": 21,
            "output_tokens": 3,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        },
        {
            "event": "llm_request",
            "provider": "anthropic",
            "model": "claude-opus-5-20260801",
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


def test_default_metric_log_contains_only_safe_structured_fields(
    monkeypatch, no_sleep, caplog,
):
    payload = _tool_payload({"answer": "patient response must not be logged"})
    payload["usage"] = {"input_tokens": 8, "output_tokens": 2}
    _patch_urlopen(monkeypatch, [payload])

    with caplog.at_level(logging.INFO, logger="cosmaneura_billing.llm"):
        out = AnthropicLLM(api_key="api-key-must-not-be-logged").complete_json(
            "patient Alice system prompt",
            "DOB 2000-01-02 user prompt",
            schema=SCHEMA,
        )

    assert out == {"answer": "patient response must not be logged"}
    records = [r for r in caplog.records if r.getMessage() == "llm_request"]
    assert len(records) == 1
    record = records[0]
    assert record.event == "llm_request"
    assert record.provider == "anthropic"
    assert record.model == "claude-opus-5"
    assert record.status == "success"
    assert record.attempt == 1
    assert isinstance(record.latency_ms, int)
    assert record.input_tokens == 8
    assert record.output_tokens == 2
    rendered = caplog.text + repr(record.__dict__)
    for secret in (
        "Alice", "DOB 2000-01-02", "api-key-must-not-be-logged",
        "patient response must not be logged",
    ):
        assert secret not in rendered


def test_failure_metric_uses_fixed_status_without_exception_text(
    monkeypatch, no_sleep,
):
    _patch_urlopen(monkeypatch, [
        urllib.error.HTTPError(
            "https://api/Patient-Jane-Doe", 401, "secret API key rejected", {},
            io.BytesIO(b"patient response"),
        ),
    ])
    metrics = []

    with pytest.raises(urllib.error.HTTPError):
        AnthropicLLM(
            api_key="secret",
            metrics_sink=metrics.append,
        ).complete_json("Jane Doe", "MRN 123", schema=SCHEMA)

    assert len(metrics) == 1
    assert metrics[0]["status"] == "http_error"
    assert metrics[0]["input_tokens"] is None
    assert metrics[0]["output_tokens"] is None
    encoded = json.dumps(metrics)
    assert "Jane" not in encoded
    assert "secret" not in encoded
    assert "MRN" not in encoded


def test_terminal_response_records_usage_and_still_fails_closed(
    monkeypatch, no_sleep,
):
    payload = {
        "stop_reason": "refusal",
        "stop_details": {"category": "safety"},
        "content": [],
        "usage": {"input_tokens": 13, "output_tokens": 1},
    }
    _patch_urlopen(monkeypatch, [payload])
    metrics = []

    with pytest.raises(TerminalLLMError, match="refusal category"):
        AnthropicLLM(api_key="test", metrics_sink=metrics.append).complete_json(
            "system", "user", schema=SCHEMA,
        )

    assert metrics[0]["status"] == "terminal_response"
    assert metrics[0]["input_tokens"] == 13
    assert metrics[0]["output_tokens"] == 1


def test_invalid_usage_and_model_values_are_not_logged_verbatim(
    monkeypatch, no_sleep,
):
    payload = _tool_payload({"answer": "ok"})
    payload["usage"] = {"input_tokens": "patient name", "output_tokens": -1}
    _patch_urlopen(monkeypatch, [payload])
    metrics = []

    out = AnthropicLLM(
        model="claude\nPatient Jane Doe",
        api_key="test",
        metrics_sink=metrics.append,
    ).complete_json("system", "user", schema=SCHEMA)

    assert out == {"answer": "ok"}
    assert metrics[0]["model"] == "invalid_model_label"
    assert metrics[0]["input_tokens"] is None
    assert metrics[0]["output_tokens"] is None
    assert "Jane Doe" not in json.dumps(metrics)


def test_metrics_failure_never_changes_inference_result(monkeypatch, no_sleep):
    _patch_urlopen(monkeypatch, [_tool_payload({"answer": "ok"})])

    def broken_sink(_metric):
        raise RuntimeError("telemetry unavailable")

    out = AnthropicLLM(
        api_key="test",
        metrics_sink=broken_sink,
    ).complete_json("system", "user", schema=SCHEMA)

    assert out == {"answer": "ok"}


def test_cache_usage_is_accounted_and_nonfinite_clock_cannot_break_inference(
    monkeypatch, no_sleep,
):
    payload = _tool_payload({"answer": "ok"})
    payload["usage"] = {
        "input_tokens": 30,
        "output_tokens": 4,
        "cache_creation_input_tokens": 12,
        "cache_read_input_tokens": 18,
    }
    _patch_urlopen(monkeypatch, [payload])
    times = iter([float("nan"), float("inf")])
    metrics = []

    out = AnthropicLLM(
        api_key="test",
        clock=lambda: next(times),
        metrics_sink=metrics.append,
    ).complete_json("system", "user", schema=SCHEMA)

    assert out == {"answer": "ok"}
    assert metrics[0]["latency_ms"] is None
    assert metrics[0]["cache_creation_input_tokens"] == 12
    assert metrics[0]["cache_read_input_tokens"] == 18


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-jane-patient",
        "claude-3-7-sonnet-20250219-jane",
        "claude-opus-5-2026080",  # date must be exactly eight digits
    ],
)
def test_metric_model_label_rejects_free_form_suffixes(
    monkeypatch, no_sleep, model,
):
    _patch_urlopen(monkeypatch, [_tool_payload({"answer": "ok"})])
    metrics = []

    AnthropicLLM(
        model=model,
        api_key="test",
        metrics_sink=metrics.append,
    ).complete_json("system", "user", schema=SCHEMA)

    assert metrics[0]["model"] == "invalid_model_label"
    assert "jane" not in json.dumps(metrics)


def test_default_client_wires_and_flushes_explicit_metric_sink(monkeypatch):
    shutdown_calls = []

    class Sink:
        def __call__(self, _event):
            pass

        def shutdown(self):
            shutdown_calls.append("shutdown")

    sink = Sink()
    observed = {}

    def create_sink(**kwargs):
        observed.update(kwargs)
        return sink

    monkeypatch.setattr(llm_module, "create_azure_monitor_llm_sink", create_sink)
    config = type("Config", (), {
        "anthropic_api_key": "synthetic-key",
        "anthropic_model": "claude-opus-5",
        "anthropic_effort": "high",
        "telemetry_enabled": True,
        "application_insights_connection_string": "synthetic-routing-metadata",
        "azure_client_id": "11111111-1111-4111-8111-111111111111",
    })()

    client = llm_module.get_default_client(config)

    assert isinstance(client, AnthropicLLM)
    assert observed == {
        "enabled": True,
        "connection_string": "synthetic-routing-metadata",
        "managed_identity_client_id": "11111111-1111-4111-8111-111111111111",
    }
    client.shutdown_metrics()
    assert shutdown_calls == ["shutdown"]


def test_default_client_fails_startup_when_enabled_metrics_cannot_initialize(
    monkeypatch,
):
    monkeypatch.setattr(
        llm_module, "create_azure_monitor_llm_sink", lambda **_kwargs: None,
    )
    config = type("Config", (), {
        "anthropic_api_key": "synthetic-key",
        "anthropic_model": "claude-opus-5",
        "anthropic_effort": "high",
        "telemetry_enabled": True,
        "application_insights_connection_string": "synthetic-routing-metadata",
        "azure_client_id": "11111111-1111-4111-8111-111111111111",
    })()

    with pytest.raises(RuntimeError, match="metrics exporter is unavailable"):
        llm_module.get_default_client(config)
