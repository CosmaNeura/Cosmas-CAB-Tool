"""Actual Twilio protocol shapes with synthetic values and injected transports only."""
import base64
from dataclasses import asdict, replace
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlencode
from xml.etree import ElementTree as ET

import pytest

from cosmaneura_billing.chase.communications.twilio import (
    HttpResponse, MAX_BODY, Submission, TwilioAdapter, TwilioConfig, TwilioError,
    gather_response_twiml, scripted_twiml, verify_form_signature,
)

ACCOUNT = "AC" + "a" * 32
MESSAGE = "SM" + "b" * 32
CALL = "CA" + "c" * 32
FROM = "+12025550101"
TO = "+12025550102"
CONFIG = TwilioConfig(ACCOUNT, "synthetic-test-token", FROM,
                      *["https://demo.example.invalid/twilio/" + path for path in
                        ("inbound", "sms-status", "voice-status", "gather")], enabled_channels=("sms", "automated_voice"))


def signed(kind, values, config=CONFIG):
    url = {"sms_inbound": config.sms_inbound_url, "sms_status": config.sms_status_url,
           "voice_status": config.voice_status_url, "voice_gather": config.voice_action_url}[kind]
    body = urlencode(values).encode()
    signature = base64.b64encode(hmac.new(config.auth_token.encode(),
        (url + "".join(k + values[k] for k in sorted(values))).encode(), hashlib.sha1).digest()).decode()
    return body, signature


def sms(status="delivered"):
    return {"AccountSid": ACCOUNT, "MessageSid": MESSAGE, "MessageStatus": status,
            "From": FROM, "To": TO}


def voice(status="in-progress"):
    return {"AccountSid": ACCOUNT, "CallSid": CALL, "CallStatus": status,
            "From": FROM, "To": TO, "Direction": "outbound-api", "SequenceNumber": "2"}


@pytest.mark.parametrize("code", [None, "", "0", "30034", "999999"])
def test_callback_error_code_is_bounded_and_vendor_message_is_discarded(code):
    values = {**sms("undelivered"), "ErrorMessage": "PRIVATE-CANARY", "MoreInfo": "https://private.invalid/PRIVATE-CANARY"}
    if code is not None:
        values["ErrorCode"] = code
    event = TwilioAdapter(CONFIG).parse_webhook("sms_status", *signed("sms_status", values))
    assert event.error_code == (code or None)
    assert "PRIVATE-CANARY" not in json.dumps(asdict(event))


@pytest.mark.parametrize("code", ["1234567", "30034\n", "true", "1.0", "private_url", "https://private.invalid"])
def test_callback_non_code_diagnostics_are_rejected(code):
    with pytest.raises(TwilioError, match="invalid_error_code"):
        TwilioAdapter(CONFIG).parse_webhook("sms_status", *signed("sms_status", {**sms("failed"), "ErrorCode": code}))


def test_official_twilio_signature_vector_is_exact_including_query():
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
              "From": "+14158675310", "To": "+18005551212"}
    url = "https://example.com/myapp.php?foo=1&bar=2"
    signature = "L/OH5YylLD5NRKLltdqwSvS0BnU="  # Published Twilio security documentation vector.
    assert verify_form_signature(url, "12345", urlencode(params).encode(), signature) == params
    with pytest.raises(TwilioError, match="invalid_signature"):
        verify_form_signature(url + "&new=1", "12345", urlencode(params).encode(), signature)


def test_transport_is_disabled_by_default_and_building_has_no_io():
    adapter = TwilioAdapter(CONFIG)
    for request in [adapter.build_sms(TO, "Synthetic reminder"), adapter.build_call(TO)]:
        with pytest.raises(TwilioError, match="transport_disabled"):
            adapter.submit(request)
    assert CONFIG.auth_token not in repr(CONFIG)
    assert "Synthetic reminder" not in repr(adapter.build_sms(TO, "Synthetic reminder"))


@pytest.mark.parametrize("channel", ["sms", "voice"])
def test_real_rest_request_shapes_and_acceptance_are_not_delivery(channel):
    requests = []
    def transport(request):
        requests.append(request)
        return HttpResponse(201, json.dumps({"sid": MESSAGE if channel == "sms" else CALL,
            "account_sid": ACCOUNT, "status": "queued", "direction": "outbound-api",
            "from": FROM, "to": TO, "error_code": None}).encode())
    adapter = TwilioAdapter(CONFIG, transport=transport)
    request = adapter.build_sms(TO, "Text & plus + with café") if channel == "sms" else adapter.build_call(TO)
    assert adapter.submit(request) == Submission("accepted", MESSAGE if channel == "sms" else CALL, "queued")
    assert requests == [request] and request.method == "POST" and not request.follow_redirects
    assert request.timeout_seconds == 10 and request.max_response_bytes == MAX_BODY
    assert request.url == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/{'Messages' if channel == 'sms' else 'Calls'}.json"
    assert dict(request.headers)["Authorization"] == "Basic " + base64.b64encode(f"{ACCOUNT}:{CONFIG.auth_token}".encode()).decode()
    fields = parse_qs(request.body.decode())
    assert fields["To"] == [TO] and fields["From"] == [FROM]
    if channel == "sms":
        assert fields["Body"] == ["Text & plus + with café"]
        assert fields["StatusCallback"] == [CONFIG.sms_status_url]
    else:
        assert fields["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
        assert fields["Record"] == ["false"] and fields["TimeLimit"] == ["60"]
        assert fields["Twiml"] == [scripted_twiml(CONFIG.voice_action_url)]


@pytest.mark.parametrize("reply,outcome,code", [
    (HttpResponse(429, b'{"code":20429,"message":"PRIVATE-CANARY"}'), "rejected", "20429"),
    (HttpResponse(500, b'PRIVATE-CANARY'), "unknown", "provider_uncertain"),
    (HttpResponse(408, b''), "unknown", "provider_uncertain"),
    (HttpResponse(302, b''), "unknown", "provider_uncertain"),
    (HttpResponse(201, b'[' * 2000), "unknown", "invalid_provider_response"),
    (HttpResponse(201, b'x' * (MAX_BODY + 1)), "unknown", "invalid_provider_response"),
    (HttpResponse(201, b'{"sid":"first","sid":"second"}'), "unknown", "invalid_provider_response"),
])
def test_provider_failures_are_bounded_sanitized_and_never_retried(reply, outcome, code):
    calls = []
    adapter = TwilioAdapter(CONFIG, transport=lambda r: calls.append(r) or reply)
    assert adapter.submit(adapter.build_sms(TO, "Synthetic")) == Submission(outcome, error_code=code)
    assert len(calls) == 1


def test_transport_exception_is_ambiguous_and_request_is_not_retried():
    calls = []
    def timeout(request):
        calls.append(request)
        raise TimeoutError("PRIVATE-CANARY")
    adapter = TwilioAdapter(CONFIG, transport=timeout)
    result = adapter.submit(adapter.build_sms(TO, "Synthetic"))
    assert result == Submission("unknown", error_code="transport_uncertain") and len(calls) == 1
    assert "PRIVATE-CANARY" not in repr(result)


@pytest.mark.parametrize("key,value", [("account_sid", "AC" + "d" * 32), ("sid", CALL),
    ("direction", "inbound"), ("to", FROM), ("from", TO), ("status", "received")])
def test_success_response_cannot_bind_a_different_account_direction_or_recipient(key, value):
    payload = {"sid": MESSAGE, "account_sid": ACCOUNT, "status": "queued", "direction": "outbound-api", "from": FROM, "to": TO}
    payload[key] = value
    adapter = TwilioAdapter(CONFIG, transport=lambda _: HttpResponse(201, json.dumps(payload).encode()))
    assert adapter.submit(adapter.build_sms(TO, "Synthetic")).outcome == "unknown"


def test_fixed_endpoint_prevents_redirecting_configured_credentials():
    calls = []
    adapter = TwilioAdapter(CONFIG, transport=lambda r: calls.append(r))
    with pytest.raises(TwilioError):
        adapter.submit(replace(adapter.build_sms(TO, "Synthetic"), url="https://other.example.invalid"))
    assert calls == []


@pytest.mark.parametrize("channel", ["sms", "voice"])
def test_mutated_request_envelope_never_reaches_transport(channel):
    calls = []
    adapter = TwilioAdapter(CONFIG, transport=lambda r: calls.append(r))
    request = adapter.build_sms(TO, "Approved synthetic message") if channel == "sms" else adapter.build_call(TO)
    mutations = [dict(to_number="+12025550103"), dict(headers=(("Authorization", "wrong"),)),
                 dict(timeout_seconds=999), dict(follow_redirects=True), dict(channel=[]),
                 dict(body=bytearray(request.body)), dict(timeout_seconds=10.0),
                 dict(body=request.body.replace(b"To=%2B12025550102", b"To=%2B12025550103")),
                 dict(body=request.body.replace(b"From=%2B12025550101", b"From=%2B12025550103"))]
    if channel == "sms":
        mutations += [dict(body=request.body.replace(b"Approved", b"Changed")),
                      dict(message_text="Changed intent without rebuilding request")]
    else:
        mutations += [dict(body=request.body.replace(b"Record=false", b"Record=true")),
                      dict(body=request.body.replace(b"Press+1", b"Changed+1")), dict(message_text="unexpected")]
    for mutation in mutations:
        with pytest.raises(TwilioError):
            adapter.submit(replace(request, **mutation))
    assert calls == []


def test_container_webhook_kind_has_a_fixed_error():
    with pytest.raises(TwilioError, match="invalid_webhook_kind"):
        TwilioAdapter(CONFIG).parse_webhook([], b"Body=test", "unused")


@pytest.mark.parametrize("raw", [b'Body=a&Body=a', b'Body=a&B%6fdy=b', b'Body=%ZZ', b'Body=%FF',
    b'Body=x&missing', b'x=1&' + b'Body=x&' * 128, b'Body=' + b'x' * MAX_BODY])
def test_duplicate_or_malformed_form_fields_are_rejected_before_normalization(raw):
    with pytest.raises(TwilioError, match="invalid_form"):
        TwilioAdapter(CONFIG).parse_webhook("sms_inbound", raw, "A" * 27 + "=")


def test_sms_inbound_keeps_text_opt_out_and_media_count_without_fetch_or_auto_reply():
    values = {"AccountSid": ACCOUNT, "MessageSid": MESSAGE, "From": TO, "To": FROM,
              "Body": " STOP ", "NumMedia": "1", "OptOutType": "STOP",
              "MediaUrl0": "https://untrusted.example.invalid/no-fetch", "NewSignedField": "value"}
    event = TwilioAdapter(CONFIG).parse_webhook("sms_inbound", *signed("sms_inbound", values))
    assert asdict(event) == {"kind":"sms_inbound", "channel":"sms", "provider_id":MESSAGE,
        "provider_status":"received", "status":"received", "from_number":TO, "to_number":FROM,
        "text":" STOP ", "disposition":None, "provider_sequence":None, "error_code":None,
        "opt_out":"STOP", "media_count":1}
    assert " STOP " not in repr(event) and "no-fetch" not in repr(event) and TO not in repr(event)
    changed = {**values, "NewSignedField":"tampered"}
    with pytest.raises(TwilioError, match="invalid_signature"):
        TwilioAdapter(CONFIG).parse_webhook("sms_inbound", urlencode(changed).encode(), signed("sms_inbound", values)[1])


@pytest.mark.parametrize("status", ["accepted", "sent", "delivered", "read", "failed"])
def test_sms_delivery_status_is_not_a_reply(status):
    event = TwilioAdapter(CONFIG).parse_webhook("sms_status", *signed("sms_status", sms(status)))
    assert event.status == event.provider_status == status
    assert event.text is None and event.disposition is None and event.kind == "sms_status"


@pytest.mark.parametrize("changes", [{"AccountSid":"AC" + "d" * 32}, {"Direction":"inbound"},
    {"MessageSid":CALL}, {"SmsSid":"SM" + "d" * 32}, {"From":TO}, {"To":"not-a-phone"},
    {"MessageStatus":"received"}, {"SmsStatus":"queued"}])
def test_even_signed_account_direction_sid_and_alias_conflicts_are_rejected(changes):
    values = {**sms(), **changes}
    with pytest.raises(TwilioError):
        TwilioAdapter(CONFIG).parse_webhook("sms_status", *signed("sms_status", values))


def test_endpoint_confusion_does_not_validate_and_status_callbacks_may_omit_numbers():
    values = {"AccountSid":ACCOUNT, "MessageSid":MESSAGE, "MessageStatus":"sent"}
    event = TwilioAdapter(CONFIG).parse_webhook("sms_status", *signed("sms_status", values))
    assert event.from_number is None and event.to_number is None
    with pytest.raises(TwilioError, match="invalid_signature"):
        TwilioAdapter(CONFIG).parse_webhook("sms_inbound", *signed("sms_status", values))


@pytest.mark.parametrize("status,normalized", [("initiated","initiated"), ("ringing","ringing"),
    ("in-progress","answered"), ("completed","completed"), ("no-answer","no-answer")])
def test_call_progress_preserves_provider_status_without_patient_disposition(status, normalized):
    event = TwilioAdapter(CONFIG).parse_webhook("voice_status", *signed("voice_status", voice(status)))
    assert event.provider_status == status and event.status == normalized
    assert event.provider_sequence == 2 and event.disposition is None and event.text is None


@pytest.mark.parametrize("digits,disposition", [("1","callback_requested"), ("2","reports_submitted"),
    ("9","stop_calls"), ("","no_input"), ("3","invalid_input")])
def test_dtmf_records_patient_statement_separately_from_call_progress(digits, disposition):
    values = {**voice(), "Digits":digits}
    event = TwilioAdapter(CONFIG).parse_webhook("voice_gather", *signed("voice_gather", values))
    assert event.disposition == disposition and event.status == "answered"
    assert event.text is None


def test_script_is_keypad_only_one_prompt_with_no_recording_or_automatic_text():
    root = ET.fromstring(scripted_twiml(CONFIG.voice_action_url + "?one=1&two=2"))
    assert [element.tag for element in root.iter()] == ["Response", "Gather", "Say", "Hangup"]
    gather = root.find("Gather")
    assert gather.attrib == {"input":"dtmf", "numDigits":"1", "timeout":"7",
        "action":CONFIG.voice_action_url + "?one=1&two=2", "method":"POST", "actionOnEmptyResult":"true"}
    assert all(f"Press {digit}" in gather.find("Say").text for digit in (1, 2, 9))
    assert [e.tag for e in ET.fromstring(gather_response_twiml()).iter()] == ["Response", "Say", "Hangup"]


@pytest.mark.parametrize("body", ["", "x" * 1601, "private\x00value", "bad\ud800"])
def test_sms_body_bounds_fail_before_transport(body):
    with pytest.raises(TwilioError):
        TwilioAdapter(CONFIG).build_sms(TO, body)
