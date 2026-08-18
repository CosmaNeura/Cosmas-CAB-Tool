"""Trusted Container Apps / Microsoft Entra identity boundary.

Azure Container Apps built-in authentication validates the Entra session before
the request reaches this container and injects ``X-MS-CLIENT-PRINCIPAL``.  The
application accepts that header only in the explicit ``container_apps`` mode;
production configuration cannot select the local development bypass.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import parse_qs
from uuid import UUID

from .security import FixedWindowLimiter, client_ip, send_json
from .settings import Settings

_PRINCIPAL_HEADER = b"x-ms-client-principal"
_PRINCIPAL_ID_HEADER = b"x-ms-client-principal-id"
_PRINCIPAL_IDP_HEADER = b"x-ms-client-principal-idp"
_OID_CLAIMS = frozenset({
    "oid",
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
})
_TENANT_CLAIMS = frozenset({
    "tid",
    "http://schemas.microsoft.com/identity/claims/tenantid",
})
_ROLE_CLAIMS = frozenset({
    "role",
    "roles",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
})
_PUBLIC_EXACT = frozenset({"/", "/index.html", "/favicon.ico", "/api/live", "/api/ready"})
_PUBLIC_PREFIXES = ("/assets/", "/.auth/")
_DEPRECATED_IDENTITY_PARAMS = frozenset({
    "user_id", "coder_id", "userid", "actor",
})


class PrincipalError(ValueError):
    """A trusted-principal header is missing required, valid claims."""


class PrincipalRoleError(PermissionError):
    """A valid Entra principal has no permitted application role."""

    def __init__(self, principal: "AuthPrincipal") -> None:
        super().__init__("principal has no permitted application role")
        self.principal = principal


class PrincipalStoreUnavailable(RuntimeError):
    """The local membership authority could not make a safe decision."""


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: str
    tenant_id: str
    practice_id: str
    roles: frozenset[str]

    def has_any_role(self, roles: Iterable[str]) -> bool:
        current = {role.casefold() for role in self.roles}
        return any(role.casefold() in current for role in roles)


def _claim_values(payload: dict, claim_types: frozenset[str]) -> list[str]:
    values: list[str] = []
    accepted = set(claim_types)
    claims = payload.get("claims")
    if not isinstance(claims, list) or len(claims) > 100:
        raise PrincipalError("invalid principal claims")
    for claim in claims:
        if not isinstance(claim, dict):
            raise PrincipalError("invalid principal claims")
        claim_type = claim.get("typ")
        value = claim.get("val")
        if claim_type in accepted and isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def parse_container_apps_principal(encoded: str, settings: Settings) -> AuthPrincipal:
    """Decode the bounded, platform-injected principal and enforce tenant/roles."""
    if not encoded or len(encoded) > 65_536:
        raise PrincipalError("invalid principal header")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded, validate=True)
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrincipalError("invalid principal header") from exc
    if not isinstance(payload, dict):
        raise PrincipalError("invalid principal header")
    auth_type = str(payload.get("auth_typ", "")).casefold()
    if auth_type not in {"aad", "azureactivedirectory"}:
        raise PrincipalError("principal is not an Entra identity")

    object_ids = _claim_values(payload, _OID_CLAIMS)
    tenant_ids = _claim_values(payload, _TENANT_CLAIMS)
    roles = frozenset(_claim_values(payload, _ROLE_CLAIMS))
    if len(object_ids) != 1 or len(tenant_ids) != 1:
        raise PrincipalError("principal identity claims are incomplete")
    try:
        object_id = str(UUID(object_ids[0]))
        tenant_id = str(UUID(tenant_ids[0]))
    except ValueError as exc:
        raise PrincipalError("principal identity claims are malformed") from exc
    if tenant_id.casefold() != settings.entra_tenant_id.casefold():
        raise PrincipalError("principal tenant is not allowed")
    principal = AuthPrincipal(
        # The tenant is validated above and retained separately. The immutable
        # Entra object ID is the stable user key within this single-tenant app.
        user_id=object_id,
        tenant_id=tenant_id,
        practice_id=settings.practice_id,
        roles=roles,
    )
    if not principal.has_any_role(settings.allowed_roles):
        raise PrincipalRoleError(principal)
    return principal


def _is_public(method: str, path: str) -> bool:
    if method == "OPTIONS":
        return True
    return method in {"GET", "HEAD"} and (
        path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)
    )


class EntraAuthMiddleware:
    """Authenticate protected paths and inject one stable, server-owned identity."""

    def __init__(
        self,
        app,
        *,
        settings: Settings,
        failure_limiter: FixedWindowLimiter,
        denied_auditor: Callable[[dict, int], None] | None = None,
        principal_authorizer: Callable[[AuthPrincipal], AuthPrincipal] | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.failure_limiter = failure_limiter
        self.denied_auditor = denied_auditor
        self.principal_authorizer = principal_authorizer

    async def _respond(
        self,
        scope: dict,
        send,
        status: int,
        payload: dict,
        *,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        try:
            if self.denied_auditor is not None:
                self.denied_auditor(scope, status)
        except Exception:
            await send_json(send, 503, {"detail": "access audit is unavailable"})
            return
        await send_json(
            send, status, payload, extra_headers=extra_headers,
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if _is_public(method, path):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        if self.settings.auth_mode == "disabled":
            state["principal"] = AuthPrincipal(
                user_id=self.settings.development_user_id,
                tenant_id="development",
                practice_id=self.settings.practice_id or "development",
                roles=frozenset(self.settings.development_roles),
            )
        else:
            try:
                headers = scope.get("headers", [])
                principal_headers = [
                    value for name, value in headers
                    if name.lower() == _PRINCIPAL_HEADER
                ]
                if len(principal_headers) != 1:
                    raise PrincipalError("principal header is missing or duplicated")
                try:
                    encoded = principal_headers[0].decode("ascii")
                except UnicodeDecodeError as exc:
                    raise PrincipalError("invalid principal header") from exc
                principal = parse_container_apps_principal(
                    encoded, self.settings
                )
                principal_id_headers = [
                    value for name, value in headers
                    if name.lower() == _PRINCIPAL_ID_HEADER
                ]
                if len(principal_id_headers) > 1:
                    raise PrincipalError("duplicate principal identity header")
                if principal_id_headers:
                    convenience_id = str(UUID(
                        principal_id_headers[0].decode("ascii")
                    ))
                    if principal.user_id != convenience_id:
                        raise PrincipalError("conflicting principal identity header")
                idp_headers = [
                    value for name, value in headers
                    if name.lower() == _PRINCIPAL_IDP_HEADER
                ]
                if len(idp_headers) > 1:
                    raise PrincipalError("duplicate identity-provider header")
                if idp_headers and idp_headers[0].decode("ascii").casefold() not in {
                    "aad", "azureactivedirectory",
                }:
                    raise PrincipalError("conflicting identity-provider header")
                if self.principal_authorizer is not None:
                    principal = self.principal_authorizer(principal)
                state["principal"] = principal
            except PrincipalRoleError as exc:
                state["principal"] = exc.principal
                await self._respond(
                    scope, send, 403, {"detail": "access is not permitted"},
                )
                return
            except PrincipalStoreUnavailable:
                await self._respond(
                    scope, send, 503, {"detail": "identity service is unavailable"},
                )
                return
            except (PrincipalError, UnicodeDecodeError, ValueError):
                allowed, retry_after = self.failure_limiter.allow(
                    ("auth", client_ip(scope)),
                    self.settings.auth_failures_per_minute,
                )
                if not allowed:
                    await self._respond(
                        scope,
                        send,
                        429,
                        {"detail": "too many authentication attempts"},
                        extra_headers=[(b"retry-after", str(retry_after).encode())],
                    )
                    return
                await self._respond(
                    scope,
                    send,
                    401,
                    {"detail": "authentication required"},
                    extra_headers=[(b"www-authenticate", b"Bearer")],
                )
                return

        try:
            query = parse_qs(
                scope.get("query_string", b"").decode("utf-8", "replace"),
                keep_blank_values=True,
                max_num_fields=100,
            )
        except ValueError:
            await self._respond(
                scope, send, 400, {"detail": "invalid query parameters"},
            )
            return
        if _DEPRECATED_IDENTITY_PARAMS.intersection(
            key.casefold() for key in query
        ):
            await self._respond(
                scope,
                send,
                400,
                {"detail": "caller-supplied identity is not accepted"},
            )
            return
        await self.app(scope, receive, send)
