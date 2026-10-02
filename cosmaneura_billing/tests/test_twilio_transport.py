"""Actual Twilio protocol requests through the bounded HTTPS client, no network."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from cosmaneura_billing.chase.communications import transport as module
from cosmaneura_billing.chase.communications.twilio import MAX_BODY, TwilioAdapter, TwilioConfig, TwilioError

ACCOUNT = "AC" + "0" * 32
CONFIG = TwilioConfig(account_sid=ACCOUNT, auth_token="synthetic-local-only", from_number="+12025550199",
    sms_inbound_url="https://callback.example.invalid/inbound", sms_status_url="https://callback.example.invalid/sms",
    voice_status_url="https://callback.example.invalid/call", voice_action_url="https://callback.example.invalid/key",
    enabled_channels=("sms", "automated_voice"))


class Bytes(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
    async def __aiter__(self):
        yield self.body


def request(channel="sms"):
    adapter = TwilioAdapter(CONFIG)
    return adapter.build_sms("+12025550100", "Synthetic reminder. Reply STOP to stop.") if channel == "sms" else adapter.build_call("+12025550100")


def setup(monkeypatch, handler):
    real_client = httpx.AsyncClient
    settings = []
    def client(**kwargs):
        settings.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return module.TwilioHttpsTransport(ACCOUNT, enabled_channels=("sms", "automated_voice")), settings


@pytest.mark.parametrize("channel", ["sms", "voice"])
def test_actual_adapter_requests_reach_transport_and_validate_acceptance(monkeypatch, channel):
    calls = []
    def wire(sent):
        calls.append(sent)
        body = {"sid": ("SM" if channel == "sms" else "CA") + "1" * 32, "account_sid": ACCOUNT,
                "direction": "outbound-api", "from": CONFIG.from_number, "to": "+12025550100", "status": "queued"}
        return httpx.Response(201, stream=Bytes(json.dumps(body).encode()))
    transport, settings = setup(monkeypatch, wire)
    outcome = TwilioAdapter(CONFIG, transport=transport).submit(request(channel))
    assert outcome.outcome == "accepted" and len(calls) == 1
    assert calls[0].method == "POST" and str(calls[0].url) == request(channel).url
    assert settings == [{"trust_env": False, "follow_redirects": False, "verify": True, "timeout": 10}]


@pytest.mark.parametrize("changes", [
    {"url": "https://attacker.invalid/"}, {"url": request().url + "?copy=1"},
    {"url": request().url.replace("https:", "http:")}, {"method": "GET"},
    {"follow_redirects": True}, {"timeout_seconds": 100}, {"max_response_bytes": MAX_BODY * 2},
    {"body": b""}, {"body": b"x" * (MAX_BODY + 1)}, {"channel": "automated_voice"},
    {"headers": request().headers + (("X-Proxy", "untrusted"),)},
    {"headers": (("Authorization", "Basic bad\nheader"),) + request().headers[1:]},
])
def test_unbounded_or_wrong_destination_request_never_reaches_wire(monkeypatch, changes):
    calls = []
    transport, _ = setup(monkeypatch, lambda sent: calls.append(sent))
    with pytest.raises(TwilioError, match="invalid_transport_request"):
        transport(replace(request(), **changes))
    assert calls == []


@pytest.mark.parametrize("status", [302, 429])
def test_redirect_and_rejection_are_returned_without_retry(monkeypatch, status):
    calls = []
    def wire(sent):
        calls.append(sent)
        return httpx.Response(status, headers={"Location": "https://attacker.invalid/"}, stream=Bytes(b"{}"))
    transport, _ = setup(monkeypatch, wire)
    assert transport(request()).status_code == status and len(calls) == 1


def test_oversize_is_unknown_and_no_retry(monkeypatch):
    calls = []
    def wire(sent):
        calls.append(sent)
        return httpx.Response(201, stream=Bytes(b"x" * (MAX_BODY + 1)))
    transport, _ = setup(monkeypatch, wire)
    assert TwilioAdapter(CONFIG, transport=transport).submit(request()).outcome == "unknown"
    assert len(calls) == 1


def test_total_deadline_cancels_slow_response_and_never_retries(monkeypatch):
    calls, canceled = [], []
    async def wire(sent):
        calls.append(sent)
        try:
            await asyncio.sleep(60)
        finally:
            canceled.append(True)
    monkeypatch.setattr(module, "REQUEST_DEADLINE", 0.01)
    transport, _ = setup(monkeypatch, wire)
    assert TwilioAdapter(CONFIG, transport=transport).submit(request()).outcome == "unknown"
    assert len(calls) == 1 and canceled == [True]


def test_default_configuration_blocks_calls_before_transport(monkeypatch):
    config = TwilioConfig(account_sid=ACCOUNT, auth_token="synthetic-local-only",
        from_number=CONFIG.from_number, sms_inbound_url=CONFIG.sms_inbound_url,
        sms_status_url=CONFIG.sms_status_url, voice_status_url=CONFIG.voice_status_url,
        voice_action_url=CONFIG.voice_action_url)
    adapter = TwilioAdapter(config)
    assert adapter.build_sms("+12025550100", "Synthetic reminder").channel == "sms"
    with pytest.raises(TwilioError, match="channel_disabled"):
        adapter.build_call("+12025550100")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("unexpected wire access"))
    with pytest.raises(TwilioError, match="invalid_transport_request"):
        module.TwilioHttpsTransport(ACCOUNT)(request("voice"))
