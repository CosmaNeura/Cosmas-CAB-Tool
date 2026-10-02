"""One bounded Twilio HTTPS request. Only the separate worker constructs this."""
from __future__ import annotations

import asyncio
import re

from .twilio import HttpRequest, HttpResponse, MAX_BODY, TwilioError
from ...communications_channels import enabled_channels as validate_enabled_channels

REQUEST_DEADLINE = 10


class TwilioHttpsTransport:
    """A total deadline includes connect, headers and body; never retry a POST."""

    def __init__(self, account_sid: str, *, enabled_channels=("sms",)):
        if not isinstance(account_sid, str) or not re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid):
            raise TwilioError("invalid_configuration")
        channels = validate_enabled_channels(enabled_channels)
        self._urls = {channel: f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/{resource}.json"
                      for channel, resource, key in (("sms", "Messages", "sms"), ("voice", "Calls", "automated_voice")) if key in channels}

    def __repr__(self):
        return "TwilioHttpsTransport(account='<bound>')"

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if (not isinstance(request, HttpRequest) or request.channel not in self._urls
                or request.url != self._urls[request.channel] or request.method != "POST"
                or request.follow_redirects is not False or request.timeout_seconds != 10
                or request.max_response_bytes != MAX_BODY or not isinstance(request.body, bytes)
                or not 0 < len(request.body) <= MAX_BODY):
            raise TwilioError("invalid_transport_request")
        try:
            headers = dict(request.headers)
            if (len(headers) != len(request.headers)
                    or set(headers) != {"Authorization", "Content-Type"}
                    or headers["Content-Type"] != "application/x-www-form-urlencoded"
                    or not re.fullmatch(r"Basic [A-Za-z0-9+/]{8,512}={0,2}", headers["Authorization"])):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise TwilioError("invalid_transport_request") from None
        try:
            return asyncio.run(self._bounded(request, headers))
        except Exception:
            # A submission may already have been accepted. The durable worker
            # records unknown and never retries this operation automatically.
            raise TwilioError("transport_outcome_unknown") from None

    async def _bounded(self, request, headers):
        return await asyncio.wait_for(self._send(request, headers), timeout=REQUEST_DEADLINE)

    async def _send(self, request, headers):
        import httpx
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=True,
                                     timeout=10) as client:
            async with client.stream("POST", request.url, headers={**headers, "Accept": "application/json"},
                                     content=request.body) as response:
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise TwilioError("transport_response_too_large")
                return HttpResponse(response.status_code, bytes(body))
