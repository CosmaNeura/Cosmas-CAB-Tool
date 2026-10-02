"""Dormant Twilio protocol adapter. Only an explicitly injected transport does I/O."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from ...communications_channels import enabled_channels
import hashlib
import hmac
import json
import re
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.etree import ElementTree as ET

MAX_BODY = 64 * 1024
SMS_STATUSES = frozenset({"accepted", "scheduled", "queued", "sending", "sent", "delivered",
                          "undelivered", "failed", "canceled", "read"})
CALL_STATUSES = frozenset({"queued", "initiated", "ringing", "in-progress", "completed",
                           "busy", "failed", "no-answer", "canceled"})
DISPOSITIONS = {"1": "callback_requested", "2": "reports_submitted", "9": "stop_calls"}


class TwilioError(ValueError):
    """Fixed codes only: never attach provider bodies, credentials, or destinations."""


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value)
            or any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise TwilioError("invalid_value")
    return value


def _sid(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[0-9a-fA-F]{32}", value):
        raise TwilioError("invalid_sid")
    return value


def _phone(value):
    if not isinstance(value, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise TwilioError("invalid_phone")
    return value


def _url(value):
    _text(value, 2048)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or any(c.isspace() for c in value) or parsed.port not in (None, 443)):
            raise ValueError()
    except ValueError:
        raise TwilioError("invalid_callback_url") from None
    return value  # The signature uses this exact configured spelling, including query.


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str = field(repr=False)
    from_number: str = field(repr=False)
    sms_inbound_url: str
    sms_status_url: str
    voice_status_url: str
    voice_action_url: str
    practice_name: str = "your practice"
    messaging_service_sid: str = ""
    enabled_channels: tuple[str, ...] = ("sms",)

    def __post_init__(self):
        try:
            object.__setattr__(self, "enabled_channels", enabled_channels(self.enabled_channels))
        except ValueError:
            raise TwilioError("invalid_configuration") from None
        if self.messaging_service_sid != "":
            _sid(self.messaging_service_sid, "MG")
        _sid(self.account_sid, "AC")
        if not isinstance(self.auth_token, str) or not re.fullmatch(r"[!-~]{1,256}", self.auth_token):
            raise TwilioError("invalid_configuration")
        _phone(self.from_number)
        if (not isinstance(self.practice_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &'().,-]{0,79}", self.practice_name)
                or self.practice_name != self.practice_name.strip()):
            raise TwilioError("invalid_practice_name")
        urls = (self.sms_inbound_url, self.sms_status_url, self.voice_status_url, self.voice_action_url)
        for value in urls:
            _url(value)
        if len(set(urls)) != 4:
            raise TwilioError("distinct_callback_urls_required")


@dataclass(frozen=True)
class HttpRequest:
    channel: str
    url: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)
    to_number: str = field(repr=False)
    message_text: str | None = field(default=None, repr=False)
    method: str = "POST"
    timeout_seconds: int = 10
    max_response_bytes: int = MAX_BODY
    follow_redirects: bool = False


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class Submission:
    outcome: str  # accepted is API acceptance, never delivery or human contact.
    provider_id: str | None = None
    provider_status: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class WebhookEvent:
    kind: str
    channel: str
    provider_id: str
    provider_status: str | None
    status: str | None
    from_number: str | None = field(repr=False)
    to_number: str | None = field(repr=False)
    text: str | None = field(default=None, repr=False)
    disposition: str | None = None
    provider_sequence: int | None = None
    error_code: str | None = None
    opt_out: str | None = None
    media_count: int = 0


def _form(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BODY:
        raise TwilioError("invalid_form")
    try:
        encoded = raw.decode("ascii")
        if re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
            raise ValueError()
        pairs = parse_qsl(encoded, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=128)
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError()  # Reject duplicate keys, even equal values; never last-wins.
        for key, value in pairs:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key):
                raise ValueError()
            _text(value, 4096, empty=True)
        return dict(pairs)
    except (ValueError, UnicodeError):
        raise TwilioError("invalid_form") from None


def verify_form_signature(public_url: str, auth_token: str, raw_form: bytes,
                          signature: str) -> dict[str, str]:
    """Form POSTs only. Unknown signed fields participate; duplicate fields fail."""
    _url(public_url)
    if not isinstance(auth_token, str) or not 1 <= len(auth_token) <= 256:
        raise TwilioError("invalid_configuration")
    params = _form(raw_form)
    if not isinstance(signature, str) or not re.fullmatch(r"[A-Za-z0-9+/]{27}=", signature):
        raise TwilioError("invalid_signature")
    canonical = public_url + "".join(key + params[key] for key in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
    if not hmac.compare_digest(expected, signature):
        raise TwilioError("invalid_signature")
    return params


def call_script_text(practice_name: str = "your practice") -> str:
    return (f"This is an automated follow-up from {practice_name}. "
        "Press 1 to request a callback. Press 2 if you have already submitted the requested information. "
        "Press 9 to stop automated calls. Please do not share medical or insurance details on this call.")


def scripted_twiml(action_url: str, practice_name: str = "your practice") -> str:
    """One DTMF prompt; no recording, speech recognition, transfer, or SMS."""
    response = ET.Element("Response")
    gather = ET.SubElement(response, "Gather", input="dtmf", numDigits="1", timeout="7",
                           action=_url(action_url), method="POST", actionOnEmptyResult="true")
    ET.SubElement(gather, "Say").text = call_script_text(practice_name)
    ET.SubElement(response, "Hangup")
    return ET.tostring(response, encoding="unicode")


def gather_response_twiml() -> str:
    """Caller stores an authenticated disposition first, then ends the call."""
    return "<Response><Say>Thank you. Goodbye.</Say><Hangup /></Response>"


def _error_code(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,6}", str(value)):
        raise TwilioError("invalid_error_code")
    return str(value)


def _json_object(body):
    def unique(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError()
        return dict(pairs)
    def reject_constant(_):
        raise ValueError()
    value = json.loads(body, object_pairs_hook=unique, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError()
    return value


class TwilioAdapter:
    def __init__(self, config: TwilioConfig, *, transport: Callable[[HttpRequest], HttpResponse] | None = None):
        if not isinstance(config, TwilioConfig) or (transport is not None and not callable(transport)):
            raise TwilioError("invalid_configuration")
        self.config, self.transport = config, transport

    def _request(self, channel, to, pairs, *, message_text=None):
        if ("sms" if channel == "sms" else "automated_voice") not in self.config.enabled_channels:
            raise TwilioError("channel_disabled")
        to = _phone(to)
        resource = "Messages" if channel == "sms" else "Calls"
        credentials = base64.b64encode(f"{self.config.account_sid}:{self.config.auth_token}".encode()).decode()
        body = urlencode([("To", to), ("From", self.config.from_number), *pairs]).encode()
        if len(body) > MAX_BODY:
            raise TwilioError("invalid_request")
        return HttpRequest(channel, f"https://api.twilio.com/2010-04-01/Accounts/{self.config.account_sid}/{resource}.json",
                           (("Authorization", "Basic " + credentials),
                            ("Content-Type", "application/x-www-form-urlencoded")), body, to,
                           message_text=message_text)

    def build_sms(self, to: str, body: str) -> HttpRequest:
        body = _text(body, 1600)
        service = [("MessagingServiceSid", self.config.messaging_service_sid)] if self.config.messaging_service_sid else []
        return self._request("sms", to, [("Body", body), ("StatusCallback", self.config.sms_status_url), *service],
                             message_text=body)

    def build_call(self, to: str) -> HttpRequest:
        return self._request("voice", to, [("Twiml", scripted_twiml(self.config.voice_action_url, self.config.practice_name)),
            ("StatusCallback", self.config.voice_status_url), ("StatusCallbackMethod", "POST"),
            *[("StatusCallbackEvent", event) for event in ("initiated", "ringing", "answered", "completed")],
            ("Record", "false"), ("Timeout", "25"), ("TimeLimit", "60")])

    def submit(self, request: HttpRequest) -> Submission:
        if self.transport is None:
            raise TwilioError("transport_disabled")
        if (not isinstance(request, HttpRequest) or not isinstance(request.channel, str)
                or request.channel not in {"sms", "voice"}):
            raise TwilioError("invalid_request")
        expected = (self.build_sms(request.to_number, request.message_text) if request.channel == "sms"
                    else self.build_call(request.to_number))
        if (not isinstance(request.body, bytes) or len(request.body) > MAX_BODY
                or type(request.timeout_seconds) is not int or type(request.max_response_bytes) is not int
                or type(request.follow_redirects) is not bool or request != expected):
            raise TwilioError("invalid_request")
        try:
            response = self.transport(request)  # Exactly one attempt. Caller must honor request limits.
        except Exception:
            return Submission("unknown", error_code="transport_uncertain")
        try:
            if (not isinstance(response, HttpResponse) or type(response.status_code) is not int
                    or not isinstance(response.body, bytes) or len(response.body) > MAX_BODY):
                raise ValueError()
            if 400 <= response.status_code < 500 and response.status_code != 408:
                try:
                    code = _error_code(_json_object(response.body).get("code"))
                except (ValueError, UnicodeError, RecursionError):
                    code = None
                return Submission("rejected", error_code=code or "provider_rejected")
            if response.status_code != 201:
                return Submission("unknown", error_code="provider_uncertain")
            value = _json_object(response.body)
            sid = _sid(value.get("sid"), "SM" if request.channel == "sms" else "CA")
            statuses = SMS_STATUSES if request.channel == "sms" else CALL_STATUSES
            if (value.get("account_sid") != self.config.account_sid or value.get("direction") != "outbound-api"
                    or value.get("from") != self.config.from_number or value.get("to") != request.to_number
                    or value.get("status") not in statuses):
                raise ValueError()
            if request.channel == "sms" and value.get("messaging_service_sid") not in (
                    (None, "") if not self.config.messaging_service_sid else (self.config.messaging_service_sid,)):
                raise ValueError()
            return Submission("accepted", sid, value["status"], _error_code(value.get("error_code")))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return Submission("unknown", error_code="invalid_provider_response")

    def parse_webhook(self, kind: str, raw_form: bytes, signature: str) -> WebhookEvent:
        urls = {"sms_inbound": self.config.sms_inbound_url, "sms_status": self.config.sms_status_url,
                "voice_status": self.config.voice_status_url, "voice_gather": self.config.voice_action_url}
        if not isinstance(kind, str) or kind not in urls:
            raise TwilioError("invalid_webhook_kind")
        p = verify_form_signature(urls[kind], self.config.auth_token, raw_form, signature)
        if p.get("AccountSid") != self.config.account_sid:
            raise TwilioError("account_mismatch")
        inbound, sms = kind == "sms_inbound", kind.startswith("sms_")
        sid = _sid(p.get("MessageSid" if sms else "CallSid"), "SM" if sms else "CA")
        for alias in (("SmsSid", "SmsMessageSid") if sms else ()):
            if alias in p and p[alias] != sid:
                raise TwilioError("sid_mismatch")
        expected_direction = "inbound" if inbound else "outbound-api"
        if ("Direction" in p and p["Direction"] != expected_direction) or (not sms and "Direction" not in p):
            raise TwilioError("direction_mismatch")
        sender = _phone(p["From"]) if "From" in p else None
        recipient = _phone(p["To"]) if "To" in p else None
        if inbound:
            if not sender or recipient != self.config.from_number:
                raise TwilioError("number_mismatch")
        elif (sender is not None and sender != self.config.from_number) or (not sms and (not sender or not recipient)):
            raise TwilioError("number_mismatch")
        status = p.get("MessageStatus") if sms else p.get("CallStatus")
        if "SmsStatus" in p and status is not None and p["SmsStatus"] != status:
            raise TwilioError("status_mismatch")
        if inbound:
            if status not in (None, "received") or p.get("SmsStatus") not in (None, "received"):
                raise TwilioError("direction_mismatch")
            body = _text(p.get("Body"), 1600, empty=True)
            media = p.get("NumMedia", "0")
            if not re.fullmatch(r"[0-9]|10", media):
                raise TwilioError("invalid_media_count")
            opt_out = p.get("OptOutType")
            if opt_out not in (None, "STOP", "START", "HELP"):
                raise TwilioError("invalid_opt_out")
            return WebhookEvent(kind, "sms", sid, "received", "received", sender, recipient,
                                text=body, opt_out=opt_out, media_count=int(media))
        if status not in (SMS_STATUSES if sms else CALL_STATUSES):
            raise TwilioError("invalid_status")
        sequence = None
        if "SequenceNumber" in p:
            if not re.fullmatch(r"[0-9]{1,9}", p["SequenceNumber"]):
                raise TwilioError("invalid_sequence")
            sequence = int(p["SequenceNumber"])
        disposition = None
        if kind == "voice_gather":
            digits = p.get("Digits", "")
            if not re.fullmatch(r"[0-9*#]?", digits):
                raise TwilioError("invalid_digits")
            disposition = DISPOSITIONS.get(digits, "no_input" if digits == "" else "invalid_input")
        normalized = "answered" if not sms and status == "in-progress" else status
        return WebhookEvent(kind, "sms" if sms else "voice", sid, status, normalized, sender, recipient,
                            disposition=disposition, provider_sequence=sequence, error_code=_error_code(p.get("ErrorCode")))
