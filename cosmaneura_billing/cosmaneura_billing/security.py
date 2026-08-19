"""Small ASGI security controls applied before request parsing or inference."""
from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Hashable
from urllib.parse import urlsplit

from .settings import Settings


async def send_json(
    send,
    status: int,
    payload: dict,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def client_ip(scope: dict) -> str:
    """Use the peer address only; untrusted forwarding headers are ignored."""
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])[:64]
    return "unknown"


class StrictOriginMiddleware:
    """Reject cross-origin browser requests instead of trusting CORS alone."""

    def __init__(
        self,
        app,
        *,
        allowed_origins: tuple[str, ...],
        denied_auditor: Callable[[dict, int], None] | None = None,
    ) -> None:
        self.app = app
        self.allowed_origins = {
            origin.rstrip("/").casefold() for origin in allowed_origins
        }
        self.denied_auditor = denied_auditor

    async def _reject(self, scope: dict, send) -> None:
        try:
            if self.denied_auditor is not None:
                self.denied_auditor(scope, 403)
        except Exception:
            await send_json(send, 503, {"detail": "access audit is unavailable"})
            return
        await send_json(send, 403, {"detail": "origin is not allowed"})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origins = [
            value for name, value in scope.get("headers", [])
            if name.lower() == b"origin"
        ]
        if len(origins) > 1:
            await self._reject(scope, send)
            return
        if origins:
            try:
                origin = origins[0].decode("ascii").rstrip("/").casefold()
                parsed = urlsplit(origin)
            except UnicodeDecodeError:
                await self._reject(scope, send)
                return
            hosts = [
                value for name, value in scope.get("headers", [])
                if name.lower() == b"host"
            ]
            same_origin = (
                len(hosts) == 1
                and parsed.scheme in {"http", "https"}
                and parsed.netloc.casefold()
                == hosts[0].decode("ascii", "ignore").casefold()
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )
            if not same_origin and origin not in self.allowed_origins:
                await self._reject(scope, send)
                return
        await self.app(scope, receive, send)


class FixedWindowLimiter:
    """Thread-safe, per-process fixed-window limiter with an injectable clock."""

    def __init__(
        self,
        *,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = window_seconds
        self.clock = clock
        self._entries: dict[Hashable, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: Hashable, limit: int) -> tuple[bool, int]:
        now = self.clock()
        window = int(now // self.window_seconds)
        with self._lock:
            previous_window, count = self._entries.get(key, (window, 0))
            if previous_window != window:
                count = 0
            count += 1
            self._entries[key] = (window, count)
            if len(self._entries) > 10_000:
                self._entries = {
                    entry_key: value
                    for entry_key, value in self._entries.items()
                    if value[0] >= window - 1
                }
        remaining = self.window_seconds - (now % self.window_seconds)
        return count <= limit, max(1, math.ceil(remaining))


class RequestBodyLimitMiddleware:
    """Enforce declared and streamed request size before Pydantic sees a body."""

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers", [])
        content_lengths = [
            value for name, value in raw_headers
            if name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await send_json(send, 400, {"detail": "invalid Content-Length"})
            return
        encodings = [
            value for name, value in raw_headers
            if name.lower() == b"content-encoding"
        ]
        if len(encodings) > 1 or (
            encodings and encodings[0].strip().lower() not in {b"", b"identity"}
        ):
            await send_json(send, 415, {"detail": "content encoding is not supported"})
            return
        declared = content_lengths[0] if content_lengths else None
        if declared is not None:
            if not declared or not declared.isdigit():
                await send_json(send, 400, {"detail": "invalid Content-Length"})
                return
            declared_bytes = int(declared)
            if declared_bytes > self.max_bytes:
                await send_json(
                    send,
                    413,
                    {"detail": "request body is too large"},
                    extra_headers=[(b"connection", b"close")],
                )
                return

        consumed = 0
        response_started = False

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise _RequestTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestTooLarge:
            if response_started:  # defensive; parsers consume before responding
                raise
            await send_json(
                send,
                413,
                {"detail": "request body is too large"},
                extra_headers=[(b"connection", b"close")],
            )


class _RequestTooLarge(Exception):
    pass


def _rate_class(method: str, path: str) -> str:
    if (
        (method == "POST" and path in {"/api/billing", "/api/completeness"})
        or path.endswith("/billing")
        or path.endswith("/coder-view")
        or path.endswith("/billing-tab")
    ):
        return "inference"
    return "general"


class PrincipalRateLimitMiddleware:
    """Bound authenticated work per principal before expensive route execution."""

    def __init__(
        self,
        app,
        *,
        settings: Settings,
        limiter: FixedWindowLimiter,
    ) -> None:
        self.app = app
        self.settings = settings
        self.limiter = limiter

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        principal = scope.get("state", {}).get("principal")
        if principal is None:
            await self.app(scope, receive, send)
            return
        rate_class = _rate_class(
            scope.get("method", "GET").upper(), scope.get("path", "")
        )
        limit = (
            self.settings.inference_requests_per_minute
            if rate_class == "inference"
            else self.settings.general_requests_per_minute
        )
        allowed, retry_after = self.limiter.allow(
            (principal.practice_id, principal.user_id, rate_class), limit
        )
        if not allowed:
            await send_json(
                send,
                429,
                {"detail": "request rate limit exceeded"},
                extra_headers=[(b"retry-after", str(retry_after).encode())],
            )
            return
        await self.app(scope, receive, send)
