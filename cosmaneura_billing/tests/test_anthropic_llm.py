"""AnthropicLLM hardening: structured output, schema validation, retries.
All HTTP is faked — no network, no key."""
import io
import json
import urllib.error
import urllib.request

import pytest

from cosmaneura_billing.llm import AnthropicLLM, validate_json

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
