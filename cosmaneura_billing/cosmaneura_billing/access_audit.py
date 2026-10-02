"""PHI-minimal access audit seam, separate from claim-decision audit trails."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from uuid import UUID

from .auth import AuthPrincipal
from .security import client_ip, send_json
from .settings import Settings

_ENCOUNTER_FIXED_ROUTES = {
    "/api/encounters/detail": "encounter.view",
    "/api/encounters/billing": "encounter.compute",
    "/api/encounters/coder-view": "encounter.compute",
    "/api/encounters/billing-tab": "encounter.compute",
    "/api/encounters/header": "billing_header.view",
    "/api/encounters/header/diagnosis-catalog": "billing_header.catalog",
    "/api/encounters/header/generate": "billing_header.compute",
    "/api/encounters/header/save": "billing_header.save",
    "/api/encounters/header/signoff": "billing_header.signoff",
}
_ATHENA_ROUTES = {
    "/api/athena/card-chase": "athena.view",
    "/api/athena/card-chase/read": "athena.refresh",
    "/api/athena/card-chase/apply": "athena.apply",
}
_CHASE_POLICY_MUTATIONS = frozenset({
    "/api/chase/policy/create",
    "/api/chase/policy/approve",
    "/api/chase/policy/activate",
    "/api/chase/policy/revoke",
})


@dataclass(frozen=True)
class AccessAuditEvent:
    event_id: str
    occurred_at: str
    practice_id: str
    actor_tenant_id: str
    actor_user_id: str
    actor_roles: tuple[str, ...]
    action: str
    resource_id: str
    route: str
    method: str
    status: int
    outcome: str
    source_ip: str


class AccessAuditStore(Protocol):
    def append(self, event: AccessAuditEvent) -> None: ...
    def healthcheck(self) -> None: ...


class FileAccessAuditStore:
    """Append-only local development/test seam; pilots use PostgreSQL."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, event: AccessAuditEvent) -> None:
        payload = (json.dumps(asdict(event), separators=(",", ":")) + "\n").encode()
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CLOEXEC | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("incomplete access-audit append")
            finally:
                os.close(descriptor)

    def healthcheck(self) -> None:
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CLOEXEC | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)


class MemoryAccessAuditStore:
    def __init__(self) -> None:
        self.events: list[AccessAuditEvent] = []
        self._lock = threading.Lock()

    def append(self, event: AccessAuditEvent) -> None:
        with self._lock:
            self.events.append(event)

    def healthcheck(self) -> None:
        return None


def _opaque_resource_id(value: str) -> str:
    """Accept only server-issued random/opaque UUIDs."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return str(UUID(value))
    except ValueError:
        return ""


def classify_access(method: str, path: str) -> tuple[str, str, str] | None:
    if path in {"/api/live", "/api/ready"}:
        return None
    if path == "/api/encounters":
        return "encounter.list", "/api/encounters", ""
    if path == "/api/session":
        return "session.view", "/api/session", ""
    if path in _ENCOUNTER_FIXED_ROUTES:
        return _ENCOUNTER_FIXED_ROUTES[path], path, ""
    if path in {"/api/chase/communications/bind", "/api/chase/communications/consent",
                "/api/chase/communications/authorize", "/api/chase/communications/cancel",
                "/api/chase/communications/resolve-unknown", "/api/chase/communications/review-event",
                "/api/chase/communications/review-unassigned-text",
                "/api/chase/communications/policy/decision"}:
        return "chase.decide", path, ""
    if path in _ATHENA_ROUTES:
        return _ATHENA_ROUTES[path], path, ""
    if path == "/api/billing":
        return "encounter.compute", "/api/billing", ""
    if path == "/api/completeness":
        return "note.compute", "/api/completeness", ""
    if path == "/api/feedback":
        return "encounter.decide", "/api/feedback", ""
    if path == "/api/feedback/summary":
        return "feedback.summary", "/api/feedback/summary", ""
    if path in {"/api/chase/worklist", "/api/chase/metrics", "/api/chase/task", "/api/chase/communications", "/api/chase/communications/review-events",
                "/api/chase/communications/unassigned-texts/count", "/api/chase/communications/unassigned-texts",
                "/api/chase/communications/unassigned-text-cases"}:
        return "chase.view", path, ""
    if path in {"/api/chase/detect", "/api/chase/demo-seed"}:
        return "chase.decide", path, ""
    if path == "/api/chase/policy/status":
        return "chase.policy.view", "/api/chase/policy/status", ""
    if path in _CHASE_POLICY_MUTATIONS:
        return "chase.policy.decide", path, ""
    if path in {"/api/chase/release-message", "/api/chase/action",
                "/api/chase/communications/action"}:
        return "chase.decide", path, ""
    if path.startswith("/api/"):
        return "api.access", "unmatched_api_route", ""
    return None


def build_access_event(
    *,
    settings: Settings,
    scope: dict,
    classified: tuple[str, str, str],
    event_id: str,
    status: int,
    outcome: str,
) -> AccessAuditEvent:
    action, route, resource_id = classified
    state = scope.get("state", {})
    principal = state.get("principal")
    if not isinstance(principal, AuthPrincipal):
        principal = None
    resource_id = _opaque_resource_id(state.get("audit_resource_id", "")) or resource_id
    return AccessAuditEvent(
        event_id=event_id,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        practice_id=(
            principal.practice_id if principal else settings.practice_id
        ),
        actor_tenant_id=(principal.tenant_id if principal else ""),
        actor_user_id=(principal.user_id if principal else ""),
        actor_roles=tuple(sorted(principal.roles)) if principal else (),
        action=action,
        resource_id=resource_id,
        route=route,
        method=scope.get("method", "GET").upper(),
        status=status,
        outcome=outcome,
        source_ip=client_ip(scope),
    )


class DeniedAccessAuditor:
    """Record middleware denials that occur before the main access auditor."""

    def __init__(self, *, settings: Settings, store: AccessAuditStore) -> None:
        self.settings = settings
        self.store = store

    def __call__(self, scope: dict, status: int) -> None:
        classified = classify_access(
            scope.get("method", "GET").upper(), scope.get("path", "")
        )
        if classified is None:
            return
        outcome = "throttled" if status == 429 else "denied"
        self.store.append(build_access_event(
            settings=self.settings,
            scope=scope,
            classified=classified,
            event_id=str(uuid4()),
            status=status,
            outcome=outcome,
        ))


class AccessAuditMiddleware:
    """Reserve an access record before work, then append its completion."""

    def __init__(self, app, *, settings: Settings, store: AccessAuditStore) -> None:
        self.app = app
        self.settings = settings
        self.store = store

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        classified = classify_access(
            scope.get("method", "GET").upper(), scope.get("path", "")
        )
        if classified is None:
            await self.app(scope, receive, send)
            return

        # The reservation is the durable proof that this actor was authorized
        # to attempt the semantic action. It must exist before a route can read
        # or mutate data; P3 makes the completion and mutation one transaction.
        event_id = str(uuid4())
        scope.setdefault("state", {})["audit_event_id"] = event_id
        try:
            self.store.append(build_access_event(
                settings=self.settings,
                scope=scope,
                classified=classified,
                event_id=event_id,
                status=0,
                outcome="attempted",
            ))
        except Exception:
            await send_json(send, 503, {"detail": "access audit is unavailable"})
            return

        messages: list[dict] = []
        status = 500

        async def buffered_send(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            messages.append(message)

        caught: Exception | None = None
        try:
            await self.app(scope, receive, buffered_send)
        except Exception as exc:  # log the attempt, then preserve exception handling
            caught = exc

        outcome = (
            "denied" if status in {401, 403}
            else "throttled" if status == 429
            else "allowed" if status < 400
            else "error"
        )
        if scope.get("state", {}).get("audit_completion_committed"):
            if caught is not None:
                raise caught
            for message in messages:
                await send(message)
            return
        try:
            self.store.append(build_access_event(
                settings=self.settings,
                scope=scope,
                classified=classified,
                event_id=event_id,
                status=status,
                outcome=outcome,
            ))
        except Exception:
            if self.settings.state_store == "postgres":
                await send_json(
                    send, 503, {"detail": "access audit is unavailable"},
                )
                return
            # The pre-route reservation is already durable. Returning the
            # buffered result avoids inviting a duplicate retry after a
            # mutation while the incomplete event flags reconciliation.
            if caught is not None:
                raise caught
            for message in messages:
                await send(message)
            return
        if caught is not None:
            raise caught
        for message in messages:
            await send(message)
