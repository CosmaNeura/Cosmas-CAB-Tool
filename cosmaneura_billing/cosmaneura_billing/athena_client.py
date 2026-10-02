"""Mock-first boundary for the future Athena sandbox read integration.

This module pins only the Preview origin, OAuth metadata locations, public
synthetic practice, and portal-selected scope verified during access review.
The application operation allowlist remains empty. A separate pure decoder at
the end of this module maps the documented, bounded synthetic patient/insurance
response shapes for staff review without activating a transport. Live operation
details still require an approved sandbox contract. The read boundary is split
into three independently testable pieces:

* :class:`MockAthenaClient` supplies deterministic pages made only from the
  repository's wholly synthetic release-candidate pack;
* :class:`AthenaHTTPTransport` is a bounded, GET-only stdlib transport behind
  an empty-until-confirmed named-operation allowlist; and
* :class:`AthenaSandboxClient` composes the transport with an explicitly
  supplied contract adapter once the real contract has been reviewed.

No factory silently selects the live transport.  Constructing the mock never
reads credentials or opens a socket, and the one-shot sync skeleton runs only
the mock in this release.

The separate staff lane at the end retains its approved Preview transport.
Its immutable practice/case contract also builds and decodes Production data.
Production transport additionally requires a server-issued, active read/storage
lease; this module does not configure an application caller or issue that lease.
"""
from __future__ import annotations

import base64
import hashlib
import re
import ssl
from html.parser import HTMLParser
from threading import RLock
import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from .corpus.demo_release_candidate import load_demo_release_candidate
from .schemas import VisitBundle
from .source_import import source_import_idempotency_key
from .athena_staff_contract import AthenaStaffScope, AthenaStaffScopeError


NORMALIZED_SCHEMA_VERSION = "cosmaneura-athena-normalized-v1"
ATHENA_PREVIEW_ORIGIN = "https://api.preview.platform.athenahealth.com"
ATHENA_PREVIEW_TOKEN_URL = ATHENA_PREVIEW_ORIGIN + "/oauth2/v1/token"
ATHENA_PREVIEW_INTROSPECTION_URL = (
    ATHENA_PREVIEW_ORIGIN + "/oauth2/v1/introspect"
)
ATHENA_PREVIEW_PRACTICE_ID = "195900"
ATHENA_PREVIEW_SCOPE = "athena/service/Athenanet.MDP.*"
ATHENA_PRODUCTION_ORIGIN = "https://api.platform.athenahealth.com"
# Production transport requires an exact active read/storage lease before any
# credential or token use, including when the HTTP boundary is injected.
_STAFF_ORIGINS = MappingProxyType({
    "preview": ATHENA_PREVIEW_ORIGIN,
    "production": ATHENA_PRODUCTION_ORIGIN,
})
MAX_PAGE_RECORDS = 100
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 1_048_576
MAX_CURSOR_BYTES = 512
MAX_TOKEN_CHARS = 16_384
# Local acceptance bound, not a claim about Athena's issued token lifetime.
MAX_TOKEN_FUTURE_SECONDS = 3_600

_PAGE_KEYS = {"schema_version", "records", "next_cursor", "checkpoint"}
_RECORD_KEYS = {"source_record_id", "source_version", "visit"}
_VISIT_KEYS = {
    "encounter_id",
    "patient_age",
    "patient_sex",
    "encounter_type",
    "place_of_service",
    "intake_text",
    "transcript",
    "signed_note",
    "provider_specialty",
    "submitted_em_code",
    "submitted_icd10",
}
_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class AthenaClientError(RuntimeError):
    """A sanitized adapter failure whose message is a stable code only."""

    code = "ATHENA_CLIENT_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class AthenaPartialResponseError(AthenaClientError):
    code = "ATHENA_RESPONSE_PARTIAL"


class AthenaUnknownFieldError(AthenaClientError):
    code = "ATHENA_RESPONSE_UNKNOWN_FIELD"


class AthenaSchemaError(AthenaClientError):
    code = "ATHENA_RESPONSE_SCHEMA_INVALID"


class AthenaAuthenticationError(AthenaClientError):
    code = "ATHENA_AUTHENTICATION_FAILED"


class AthenaRequestRejected(AthenaClientError):
    code = "ATHENA_REQUEST_REJECTED"


class AthenaTimeoutError(AthenaClientError):
    code = "ATHENA_REQUEST_TIMEOUT"


class AthenaRetryExhausted(AthenaClientError):
    code = "ATHENA_RETRY_EXHAUSTED"


class AthenaOperationUnavailable(AthenaClientError):
    code = "ATHENA_OPERATION_UNAVAILABLE"


def _safe_string(value: Any, *, minimum: int = 1, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise AthenaSchemaError() from None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AthenaSchemaError() from None
    if not minimum <= len(encoded) <= maximum:
        raise AthenaSchemaError() from None
    return value


def _exact_object(
    value: Any,
    keys: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AthenaSchemaError() from None
    if keys - set(value):
        raise AthenaPartialResponseError() from None
    if set(value) - keys:
        raise AthenaUnknownFieldError() from None
    return value


def _bounded_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise AthenaSchemaError() from None
    if any(ord(char) == 0 for char in value):
        raise AthenaSchemaError() from None
    return value


def _idempotency_key(
    practice_id: str,
    source_record_id: str,
    source_version: str,
) -> str:
    return source_import_idempotency_key(
        practice_id=practice_id,
        source_system="athena_preview",
        feed="encounters",
        source_record_id=source_record_id,
        source_version=source_version,
        mapping_version=NORMALIZED_SCHEMA_VERSION,
    )


@dataclass(frozen=True, repr=False)
class AthenaVisitRecord:
    """One normalized read record bound to server-owned practice context.

    ``target_practice_id`` is supplied to the parser by the sync process.  It
    is never read from the vendor payload.  The representation is redacted so
    an accidental ``%r`` log cannot emit source or encounter identifiers.
    """

    target_practice_id: str
    source_record_id: str
    source_version: str
    mapping_version: str
    idempotency_key: str
    visit: VisitBundle

    def __repr__(self) -> str:
        return "AthenaVisitRecord(<redacted>)"


@dataclass(frozen=True, repr=False)
class AthenaPage:
    records: tuple[AthenaVisitRecord, ...]
    next_cursor: str | None
    checkpoint: str

    def __repr__(self) -> str:
        return f"AthenaPage(record_count={len(self.records)})"


@dataclass(frozen=True, repr=False)
class AthenaReadResult:
    records: tuple[AthenaVisitRecord, ...]
    page_count: int
    replay_count: int
    checkpoint: str

    def __repr__(self) -> str:
        return (
            "AthenaReadResult("
            f"record_count={len(self.records)}, page_count={self.page_count}, "
            f"replay_count={self.replay_count})"
        )


class AthenaClient(Protocol):
    mode: str

    def read_page(self, cursor: str | None = None) -> AthenaPage:
        """Read one bounded page using an opaque continuation cursor."""


def parse_normalized_page(
    payload: Any,
    *,
    server_practice_id: str,
) -> AthenaPage:
    """Parse the internal normalized seam used by the deterministic mock.

    This is not asserted to be an Athena wire schema.  The reviewed live
    contract adapter will translate vendor payloads into these values.  Exact
    keys and conservative field bounds make partial or drifted fixtures fail
    closed rather than silently importing incomplete clinical data.
    """

    practice_id = _safe_string(server_practice_id, maximum=256)
    page = _exact_object(payload, _PAGE_KEYS)
    if page["schema_version"] != NORMALIZED_SCHEMA_VERSION:
        raise AthenaSchemaError() from None

    raw_records = page["records"]
    if not isinstance(raw_records, list) or len(raw_records) > MAX_PAGE_RECORDS:
        raise AthenaSchemaError() from None

    next_cursor = page["next_cursor"]
    if next_cursor is not None:
        next_cursor = _safe_string(next_cursor, maximum=MAX_CURSOR_BYTES)
    checkpoint = _safe_string(page["checkpoint"], maximum=MAX_CURSOR_BYTES)

    records: list[AthenaVisitRecord] = []
    for raw_record in raw_records:
        record = _exact_object(raw_record, _RECORD_KEYS)
        source_record_id = _safe_string(
            record["source_record_id"], maximum=256,
        )
        source_version = _safe_string(record["source_version"], maximum=256)
        raw_visit = _exact_object(record["visit"], _VISIT_KEYS)

        age = raw_visit["patient_age"]
        if isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 125:
            raise AthenaSchemaError() from None

        submitted_em = raw_visit["submitted_em_code"]
        if submitted_em is not None:
            submitted_em = _safe_string(submitted_em, maximum=32)

        submitted_icd10 = raw_visit["submitted_icd10"]
        if (
            not isinstance(submitted_icd10, list)
            or len(submitted_icd10) > 50
        ):
            raise AthenaSchemaError() from None
        parsed_icd10 = [
            _safe_string(code, maximum=16) for code in submitted_icd10
        ]

        intake = _bounded_text(raw_visit["intake_text"], 32_768)
        transcript = _bounded_text(raw_visit["transcript"], 131_072)
        signed_note = _bounded_text(raw_visit["signed_note"], 131_072)
        if len(intake) + len(transcript) + len(signed_note) > 262_144:
            raise AthenaSchemaError() from None

        visit = VisitBundle(
            encounter_id=_safe_string(
                raw_visit["encounter_id"], maximum=256,
            ),
            patient_age=age,
            patient_sex=_safe_string(raw_visit["patient_sex"], maximum=32),
            encounter_type=_safe_string(
                raw_visit["encounter_type"], maximum=64,
            ),
            place_of_service=_safe_string(
                raw_visit["place_of_service"], maximum=8,
            ),
            intake_text=intake,
            transcript=transcript,
            signed_note=signed_note,
            provider_specialty=_safe_string(
                raw_visit["provider_specialty"], maximum=128,
            ),
            submitted_em_code=submitted_em,
            submitted_icd10=parsed_icd10,
        )
        records.append(AthenaVisitRecord(
            target_practice_id=practice_id,
            source_record_id=source_record_id,
            source_version=source_version,
            mapping_version=NORMALIZED_SCHEMA_VERSION,
            idempotency_key=_idempotency_key(
                practice_id,
                source_record_id,
                source_version,
            ),
            visit=visit,
        ))

    return AthenaPage(
        records=tuple(records),
        next_cursor=next_cursor,
        checkpoint=checkpoint,
    )


def read_all_pages(
    client: AthenaClient,
    *,
    starting_cursor: str | None = None,
    max_pages: int = MAX_PAGES,
) -> AthenaReadResult:
    """Collect a bounded page sequence and collapse exact replay records."""

    if isinstance(max_pages, bool) or not isinstance(max_pages, int):
        raise AthenaSchemaError() from None
    if not 1 <= max_pages <= MAX_PAGES:
        raise AthenaSchemaError() from None
    if starting_cursor is not None:
        _safe_string(starting_cursor, maximum=MAX_CURSOR_BYTES)

    cursor = starting_cursor
    seen_cursors: set[str] = set()
    by_idempotency_key: dict[str, AthenaVisitRecord] = {}
    ordered: list[AthenaVisitRecord] = []
    replay_count = 0
    checkpoint = ""

    for page_count in range(1, max_pages + 1):
        page = client.read_page(cursor)
        if not isinstance(page, AthenaPage):
            raise AthenaSchemaError() from None
        checkpoint = page.checkpoint
        for record in page.records:
            existing = by_idempotency_key.get(record.idempotency_key)
            if existing is None:
                by_idempotency_key[record.idempotency_key] = record
                ordered.append(record)
            elif existing == record:
                replay_count += 1
            else:
                # A vendor id/version pair cannot name two different payloads.
                raise AthenaSchemaError() from None

        next_cursor = page.next_cursor
        if next_cursor is None:
            return AthenaReadResult(
                records=tuple(ordered),
                page_count=page_count,
                replay_count=replay_count,
                checkpoint=checkpoint,
            )
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise AthenaSchemaError() from None
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    raise AthenaSchemaError() from None


class MockAthenaClient:
    """Deterministic, socket-free client backed by the four-case demo pack."""

    mode = "athena_mock"

    def __init__(self, *, server_practice_id: str, page_size: int = 2) -> None:
        self._server_practice_id = _safe_string(
            server_practice_id,
            maximum=256,
        )
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_RECORDS
        ):
            raise AthenaSchemaError() from None
        self._page_size = page_size
        pack = load_demo_release_candidate()
        self._records: tuple[dict[str, Any], ...] = tuple(
            self._mock_record(case) for case in pack["cases"]
        )

    @staticmethod
    def _mock_record(case: Mapping[str, Any]) -> dict[str, Any]:
        visit = dict(case["visit"])
        canonical = json.dumps(
            visit,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "source_record_id": f"athena-synthetic:{case['case_id']}",
            "source_version": hashlib.sha256(canonical).hexdigest(),
            "visit": visit,
        }

    def read_page(self, cursor: str | None = None) -> AthenaPage:
        if cursor is None:
            start = 0
        else:
            _safe_string(cursor, maximum=MAX_CURSOR_BYTES)
            prefix = "mock-cursor-v1-"
            if not cursor.startswith(prefix):
                raise AthenaSchemaError() from None
            try:
                start = int(cursor[len(prefix):])
            except (TypeError, ValueError):
                raise AthenaSchemaError() from None
            if start <= 0 or start >= len(self._records):
                raise AthenaSchemaError() from None

        end = min(start + self._page_size, len(self._records))
        next_cursor = (
            f"mock-cursor-v1-{end}" if end < len(self._records) else None
        )
        payload = {
            "schema_version": NORMALIZED_SCHEMA_VERSION,
            "records": list(self._records[start:end]),
            "next_cursor": next_cursor,
            "checkpoint": f"mock-checkpoint-v1-{end}",
        }
        return parse_normalized_page(
            payload,
            server_practice_id=self._server_practice_id,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward an Athena bearer token to a redirected target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_athena_https_url(value: str, *, base_url: str | None = None) -> str:
    """Validate an explicit HTTPS endpoint without reflecting it in errors."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise AthenaSchemaError() from None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise AthenaSchemaError() from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise AthenaSchemaError() from None

    if base_url is not None:
        validated_base = validate_athena_https_url(base_url)
        base = urllib.parse.urlsplit(validated_base)
        base_path = base.path.rstrip("/")
        target_path = parsed.path.rstrip("/")
        if (
            parsed.scheme.lower() != base.scheme.lower()
            or (parsed.hostname or "").casefold()
            != (base.hostname or "").casefold()
            or not (
                target_path == base_path
                or target_path.startswith(base_path + "/")
            )
        ):
            raise AthenaSchemaError() from None
    return value


def validate_athena_preview_resource_url(value: str) -> str:
    """Validate one canonical resource URL on the fixed Preview origin.

    The operation contract creates URLs from literal path templates.  This
    second check is intentionally redundant so a manually constructed or
    mutated request cannot use credentials on another origin, a traversal
    path, or an encoded-path alias.
    """

    validated = validate_athena_https_url(
        value,
        base_url=ATHENA_PREVIEW_ORIGIN,
    )
    parsed = urllib.parse.urlsplit(validated)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.preview.platform.athenahealth.com"
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or "//" in parsed.path
        or "\\" in parsed.path
        or "%" in parsed.path
    ):
        raise AthenaSchemaError() from None
    segments = parsed.path.split("/")[1:]
    if not segments or any(segment in {"", ".", ".."} for segment in segments):
        raise AthenaSchemaError() from None
    return validated


class AthenaPreviewOperation(str, Enum):
    """Semantic read operations awaiting vendor endpoint confirmation.

    Names here describe the requested workflow; they do not assert endpoint
    paths or query parameter names.  An operation is unusable until a reviewed
    literal spec is added to ``_ATHENA_PREVIEW_OPERATION_ALLOWLIST``.
    """

    PRACTICE_REFERENCE = "practice_reference"
    DEPARTMENT_REFERENCE = "department_reference"
    PATIENT_LOOKUP = "patient_lookup"
    APPOINTMENTS = "appointments"
    ENCOUNTERS = "encounters"
    ENCOUNTER_DOCUMENTATION = "encounter_documentation"
    ENCOUNTER_SERVICES = "encounter_services"
    COVERAGE = "coverage"
    CLAIMS = "claims"
    CLAIM_DETAIL = "claim_detail"
    CLAIM_TRANSACTIONS = "claim_transactions"


@dataclass(frozen=True)
class _AthenaPreviewOperationSpec:
    path_template: str
    query_keys: frozenset[str]


# Deliberately empty.  The authenticated scope review confirmed the Preview
# origin/practice/scope, but not a minimum set of proprietary endpoint paths or
# their query keys.  Tests inject a private fixture spec to exercise the guard;
# production operations remain fail-closed until vendor confirmation.
_ATHENA_PREVIEW_OPERATION_ALLOWLIST: Mapping[
    AthenaPreviewOperation,
    _AthenaPreviewOperationSpec,
] = MappingProxyType({})


def _canonical_preview_query(
    query: Mapping[str, str] | None,
    *,
    allowed_keys: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    if query is None:
        return ()
    if not isinstance(query, Mapping):
        raise AthenaSchemaError() from None
    normalized: list[tuple[str, str]] = []
    for key, value in query.items():
        if key not in allowed_keys:
            raise AthenaSchemaError() from None
        safe_key = _safe_string(key, maximum=64)
        safe_value = _safe_string(value, maximum=MAX_CURSOR_BYTES)
        if (
            any(not 33 <= ord(char) <= 126 for char in safe_key)
            or any(not 33 <= ord(char) <= 126 for char in safe_value)
            or any(char in safe_value for char in ("/", "\\", "%"))
        ):
            raise AthenaSchemaError() from None
        normalized.append((safe_key, safe_value))
    if len(normalized) != len(set(key for key, _ in normalized)):
        raise AthenaSchemaError() from None
    return tuple(sorted(normalized))


@dataclass(frozen=True, repr=False)
class AthenaRequest:
    """One canonical GET generated by the named Preview operation contract."""

    operation: AthenaPreviewOperation
    url: str
    query: tuple[tuple[str, str], ...] = ()
    method: str = "GET"

    def __repr__(self) -> str:
        return "AthenaRequest(<redacted>)"


@dataclass(frozen=True, kw_only=True)
class AthenaPreviewOperationContract:
    """Build only literal, vendor-confirmed AthenaOne Preview reads."""

    vendor_practice_id: str = ATHENA_PREVIEW_PRACTICE_ID

    def __post_init__(self) -> None:
        if self.vendor_practice_id != ATHENA_PREVIEW_PRACTICE_ID:
            raise AthenaSchemaError() from None

    def build_request(
        self,
        operation: AthenaPreviewOperation,
        *,
        query: Mapping[str, str] | None = None,
    ) -> AthenaRequest:
        if not isinstance(operation, AthenaPreviewOperation):
            raise AthenaSchemaError() from None
        spec = _ATHENA_PREVIEW_OPERATION_ALLOWLIST.get(operation)
        if spec is None:
            raise AthenaOperationUnavailable() from None
        template = spec.path_template
        placeholder = "{vendor_practice_id}"
        if (
            not isinstance(template, str)
            or template.count(placeholder) != 1
            or not template.startswith("/")
            or "//" in template
            or "\\" in template
            or "%" in template
            or "?" in template
            or "#" in template
        ):
            raise AthenaSchemaError() from None
        canonical_query = _canonical_preview_query(
            query,
            allowed_keys=spec.query_keys,
        )
        path = template.replace(placeholder, self.vendor_practice_id)
        url = ATHENA_PREVIEW_ORIGIN + path
        if canonical_query:
            url += "?" + urllib.parse.urlencode(canonical_query)
        validate_athena_preview_resource_url(url)
        return AthenaRequest(
            operation=operation,
            url=url,
            query=canonical_query,
        )

    def validate_request(self, request: AthenaRequest) -> str:
        """Rebuild and compare a request before any token is obtained."""

        if not isinstance(request, AthenaRequest) or request.method != "GET":
            raise AthenaSchemaError() from None
        try:
            query = dict(request.query)
        except (TypeError, ValueError):
            raise AthenaSchemaError() from None
        rebuilt = self.build_request(
            request.operation,
            query=query,
        )
        if request != rebuilt:
            raise AthenaSchemaError() from None
        return validate_athena_preview_resource_url(request.url)


def _validated_now(value: float | int) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not 0 <= float(value) < 10**11
    ):
        raise AthenaAuthenticationError() from None
    return float(value)


def _validate_exact_preview_scope(value: Any, requested_scope: str) -> str:
    if (
        requested_scope != ATHENA_PREVIEW_SCOPE
        or value != requested_scope
        or not isinstance(value, str)
    ):
        raise AthenaAuthenticationError() from None
    return value


@dataclass(frozen=True, repr=False)
class AthenaAccessToken:
    """Validated token metadata; representation never includes the bearer."""

    access_token: str
    scope: str
    expires_at: float

    def __repr__(self) -> str:
        return "AthenaAccessToken(<redacted>)"


@dataclass(frozen=True, repr=False)
class AthenaTokenIntrospection:
    active: bool
    scope: str
    expires_at: float
    client_id: str | None

    def __repr__(self) -> str:
        return "AthenaTokenIntrospection(<redacted>)"


def validate_athena_token_response(
    payload: Mapping[str, Any],
    *,
    requested_scope: str,
    now: float | int,
) -> AthenaAccessToken:
    """Validate a decoded OAuth response without performing an exchange."""

    if not isinstance(payload, Mapping):
        raise AthenaAuthenticationError() from None
    current = _validated_now(now)
    token = payload.get("access_token")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_TOKEN_CHARS
        or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in token)
    ):
        raise AthenaAuthenticationError() from None
    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        raise AthenaAuthenticationError() from None
    expires_in = payload.get("expires_in")
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 1 <= expires_in <= MAX_TOKEN_FUTURE_SECONDS
    ):
        raise AthenaAuthenticationError() from None
    scope = _validate_exact_preview_scope(
        payload.get("scope"),
        requested_scope,
    )
    return AthenaAccessToken(
        access_token=token,
        scope=scope,
        expires_at=current + expires_in,
    )


def validate_athena_token_introspection(
    payload: Mapping[str, Any],
    *,
    requested_scope: str,
    expected_client_id: str,
    now: float | int,
) -> AthenaTokenIntrospection:
    """Validate required introspection claims, leaving aud/iss unresolved.

    Athena's documented audience and issuer semantics have not been confirmed,
    so this function deliberately makes no claim about them.  Live execution
    remains blocked until that contract is reviewed.
    """

    if not isinstance(payload, Mapping) or payload.get("active") is not True:
        raise AthenaAuthenticationError() from None
    current = _validated_now(now)
    scope = _validate_exact_preview_scope(
        payload.get("scope"),
        requested_scope,
    )
    expires_at = payload.get("exp")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (float, int))
        or not current < float(expires_at)
        or float(expires_at) - current > MAX_TOKEN_FUTURE_SECONDS
    ):
        raise AthenaAuthenticationError() from None
    try:
        _safe_string(expected_client_id, maximum=512)
    except AthenaClientError:
        raise AthenaAuthenticationError() from None
    client_id = payload.get("client_id")
    if client_id is not None and client_id != expected_client_id:
        raise AthenaAuthenticationError() from None
    if client_id is not None:
        try:
            _safe_string(client_id, maximum=512)
        except AthenaClientError:
            raise AthenaAuthenticationError() from None
    return AthenaTokenIntrospection(
        active=True,
        scope=scope,
        expires_at=float(expires_at),
        client_id=client_id,
    )


class AthenaHTTPTransport:
    """GET-only Preview transport behind the named-operation allowlist."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], AthenaAccessToken],
        operation_contract: AthenaPreviewOperationContract | None = None,
        origin: str = ATHENA_PREVIEW_ORIGIN,
        max_retries: int = 2,
        timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if origin != ATHENA_PREVIEW_ORIGIN:
            raise AthenaSchemaError() from None
        self.base_url = ATHENA_PREVIEW_ORIGIN
        if not callable(token_provider):
            raise AthenaAuthenticationError() from None
        contract = operation_contract or AthenaPreviewOperationContract()
        if not isinstance(contract, AthenaPreviewOperationContract):
            raise AthenaSchemaError() from None
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 5
        ):
            raise AthenaSchemaError() from None
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < float(timeout) <= 120
        ):
            raise AthenaSchemaError() from None
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise AthenaSchemaError() from None

        self._token_provider = token_provider
        self._operation_contract = contract
        self._max_retries = max_retries
        self._timeout = float(timeout)
        self._max_response_bytes = max_response_bytes
        self._urlopen = urlopen or urllib.request.build_opener(
            _NoRedirectHandler(),
        ).open
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time

    def _bearer(self) -> str:
        try:
            token = self._token_provider()
        except Exception:
            raise AthenaAuthenticationError() from None
        current = _validated_now(self._clock())
        if (
            not isinstance(token, AthenaAccessToken)
            or token.scope != ATHENA_PREVIEW_SCOPE
            or isinstance(token.expires_at, bool)
            or not isinstance(token.expires_at, (float, int))
            or not current < token.expires_at
            or token.expires_at - current > MAX_TOKEN_FUTURE_SECONDS
            or not isinstance(token.access_token, str)
            or not token.access_token
            or len(token.access_token) > MAX_TOKEN_CHARS
            or any(
                char.isspace() or ord(char) < 33 or ord(char) == 127
                for char in token.access_token
            )
        ):
            raise AthenaAuthenticationError() from None
        return token.access_token

    def request_json(
        self,
        request_spec: AthenaRequest,
    ) -> dict[str, Any]:
        target = self._operation_contract.validate_request(request_spec)

        saw_timeout = False
        for attempt in range(self._max_retries + 1):
            # Revalidate or refresh on every attempt. A token that was valid on
            # attempt one may expire during a retry backoff.
            request = urllib.request.Request(
                target,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._bearer()}",
                    "User-Agent": "cosmaneura-athena-sandbox/1",
                },
                method="GET",
            )
            try:
                with self._urlopen(request, timeout=self._timeout) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            announced = int(content_length)
                        except (TypeError, ValueError):
                            raise AthenaSchemaError() from None
                        if announced < 0 or announced > self._max_response_bytes:
                            raise AthenaSchemaError() from None
                    raw = response.read(self._max_response_bytes + 1)
                    if len(raw) > self._max_response_bytes:
                        raise AthenaSchemaError() from None
                    try:
                        payload = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise AthenaSchemaError() from None
                    if not isinstance(payload, dict):
                        raise AthenaSchemaError() from None
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AthenaAuthenticationError() from None
                if exc.code not in _RETRYABLE_HTTP_STATUS:
                    raise AthenaRequestRejected() from None
                saw_timeout = saw_timeout or exc.code == 408
            except (socket.timeout, TimeoutError):
                saw_timeout = True
            except urllib.error.URLError:
                pass

            if attempt < self._max_retries:
                self._sleep(min(2 ** attempt, 8))

        if saw_timeout:
            raise AthenaTimeoutError() from None
        raise AthenaRetryExhausted() from None


class AthenaContract(Protocol):
    """Vendor response behavior to implement only after contract approval."""

    operation: AthenaPreviewOperation

    def build_page_query(
        self,
        cursor: str | None,
    ) -> Mapping[str, str] | None:
        ...

    def decode_page(
        self,
        payload: Mapping[str, Any],
        *,
        server_practice_id: str,
    ) -> AthenaPage:
        ...


class AthenaSandboxClient:
    """Compose reviewed vendor behavior with the hardened HTTP transport."""

    mode = "athena_sandbox"

    def __init__(
        self,
        *,
        transport: AthenaHTTPTransport,
        operation_contract: AthenaPreviewOperationContract,
        contract: AthenaContract,
        server_practice_id: str,
    ) -> None:
        if not isinstance(transport, AthenaHTTPTransport):
            raise AthenaSchemaError() from None
        if not isinstance(operation_contract, AthenaPreviewOperationContract):
            raise AthenaSchemaError() from None
        if transport._operation_contract is not operation_contract:
            raise AthenaSchemaError() from None
        self._transport = transport
        self._operation_contract = operation_contract
        self._contract = contract
        self._server_practice_id = _safe_string(
            server_practice_id,
            maximum=256,
        )

    def read_page(self, cursor: str | None = None) -> AthenaPage:
        if cursor is not None:
            _safe_string(cursor, maximum=MAX_CURSOR_BYTES)
        try:
            operation = self._contract.operation
            query = self._contract.build_page_query(cursor)
            request = self._operation_contract.build_request(
                operation,
                query=query,
            )
        except AthenaClientError:
            raise
        except Exception:
            raise AthenaSchemaError() from None
        if not isinstance(request, AthenaRequest):
            raise AthenaSchemaError() from None

        payload = self._transport.request_json(request)
        try:
            page = self._contract.decode_page(
                payload,
                server_practice_id=self._server_practice_id,
            )
        except AthenaClientError:
            raise
        except Exception:
            raise AthenaSchemaError() from None
        if not isinstance(page, AthenaPage):
            raise AthenaSchemaError() from None
        if any(
            record.target_practice_id != self._server_practice_id
            for record in page.records
        ):
            raise AthenaSchemaError() from None
        return page


# Documented patient/insurance fields for the dormant Card Chase review mapper.
# No operation is added to the live allowlist and no token/transport is selected.
_CARD_PATIENT_FIELDS = (
    ("first_name", "firstname"), ("last_name", "lastname"), ("birth_date", "dob"),
    ("registration_department_id", "departmentid"),
    ("current_department_id", "primarydepartmentid"),
    ("mobile_phone", "mobilephone"), ("home_phone", "homephone"),
    ("email", "email"), ("email_exists", "emailexistsyn"),
    ("text_consent", "consenttotext"), ("call_consent", "consenttocall"),
    ("billing_sms_preference", "contactpreference_billing_sms"),
    ("billing_phone_preference", "contactpreference_billing_phone"),
    ("billing_email_preference", "contactpreference_billing_email"),
)
_CARD_INSURANCE_FIELDS = (
    ("policy_record_id", "insuranceid"), ("package_id", "insurancepackageid"),
    ("package_payer_id", "insurancepackagepayerid"), ("category_id", "ircid"),
    ("member_id", "insuranceidnumber"), ("insured_id", "insuredidnumber"),
    ("group_number", "policynumber"), ("category_name", "ircname"),
    ("plan_name", "insuranceplanname"), ("plan_display_name", "insuranceplandisplayname"),
    ("payer_name", "insurancepayername"),
    ("subscriber_display_name", "insurancepolicyholder"),
    ("subscriber_first_name", "insurancepolicyholderfirstname"),
    ("subscriber_last_name", "insurancepolicyholderlastname"),
    ("relationship", "relationshiptoinsured"), ("relationship_id", "relationshiptoinsuredid"),
    ("sequence", "sequencenumber"), ("issue_date", "issuedate"),
    ("policy_expiration_date", "expirationdate"), ("cancelled", "cancelled"),
    ("source_updated_at", "lastupdated"), ("package_expiration_date", "packageexpirationdate"),
    ("eligibility_status", "eligibilitystatus"),
    ("eligibility_checked_at", "eligibilitylastchecked"),
    ("eligibility_message", "eligibilitymessage"), ("eligibility_reason", "eligibilityreason"),
)


@dataclass(frozen=True, repr=False)
class AthenaCardField:
    name: str
    pointer: str
    present: bool
    wire_type: str
    value: str | int | bool | None


@dataclass(frozen=True, repr=False)
class AthenaCardRead:
    dataset: str
    sha256: str
    records: tuple[tuple[AthenaCardField, ...], ...]
    total_count: int | None
    truncated: bool


def _card_json(raw: bytes, *, text_limit: int = 8192, allow_lines: bool = False) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError()

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 262_144:
        raise AthenaSchemaError() from None
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                             parse_constant=invalid_constant)
        pending, nodes = [(payload, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 16 or nodes > 20_000:
                raise ValueError()
            if isinstance(item, str):
                if len(item.encode("utf-8")) > text_limit or any(
                    (ord(char) < 32 and not (allow_lines and char in "\n\r\t"))
                    or ord(char) == 127 for char in item
                ):
                    raise ValueError()
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError()
            elif isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
                pending.extend((key, depth + 1) for key in item)
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return payload
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise AthenaSchemaError() from None


def decode_athena_card_read(
    raw: bytes, *, dataset: str, expected_patient_id: str, scope: AthenaStaffScope | None = None,
) -> AthenaCardRead:
    """Decode supplied bytes only; this function cannot read or authorize data.

    Insurance items need not repeat patientid. Their request binding must come
    from the trusted caller's two-read receipt; an explicit mismatch always
    rejects. Unknown fields are excluded, including emergency-contact fields,
    SSNs, balances, addresses and any purported consent/coverage shortcuts.
    Partial pages remain partial; continuation URLs never escape this decoder.
    """
    if not isinstance(dataset, str) or dataset not in {"patient", "insurance"}:
        raise AthenaSchemaError() from None
    _staff_patient(expected_patient_id, scope=scope)
    practice = _staff_practice(scope)
    payload = _card_json(raw)
    if dataset == "patient":
        records, total, truncated = payload, None, False
        fields, prefix, limit = _CARD_PATIENT_FIELDS, "", 1
    else:
        if not isinstance(payload, dict) or "insurances" not in payload:
            raise AthenaSchemaError() from None
        if ("patientid" in payload and payload["patientid"] != expected_patient_id
                or "practiceid" in payload and payload["practiceid"] != practice):
            raise AthenaSchemaError() from None
        records, total = payload["insurances"], payload.get("totalcount")
        if "totalcount" in payload and (type(total) is not int or not 0 <= total <= 1_000_000):
            raise AthenaSchemaError() from None
        for key in ("next", "previous"):
            if payload.get(key) is not None:
                _safe_string(payload[key], minimum=0, maximum=2048)
        truncated = bool(payload.get("next")) or bool(payload.get("previous")) or total is None
        fields, prefix, limit = _CARD_INSURANCE_FIELDS, "/insurances", 5
    if not isinstance(records, list) or len(records) > limit:
        raise AthenaSchemaError() from None
    if total is not None and total < len(records):
        raise AthenaSchemaError() from None
    truncated = truncated or (total is not None and total > len(records))
    output = []
    types = {str: "string", int: "number", float: "number", bool: "boolean",
             dict: "object", list: "array", type(None): "null"}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise AthenaSchemaError() from None
        if ((dataset == "patient" or "patientid" in record)
                and record.get("patientid") != expected_patient_id):
            raise AthenaSchemaError() from None
        if "practiceid" in record and record["practiceid"] != practice:
            raise AthenaSchemaError() from None
        output.append(tuple(AthenaCardField(
            name=name, pointer=f"{prefix}/{index}/{key}", present=key in record,
            wire_type=types[type(record.get(key))] if key in record else "absent",
            value=record.get(key) if type(record.get(key)) in {str, int, bool} else None,
        ) for name, key in fields))
    return AthenaCardRead(dataset, hashlib.sha256(raw).hexdigest(), tuple(output),
                          total, bool(truncated))

# Explicit staff-reviewed Preview application lane. This does NOT register an
# operation in the old sync allowlist or alter the separately approved probe.
_STAFF_FIELDS = ("encounterid", "appointmentid", "departmentid", "providerid",
                 "encounterdate", "encountertype", "status", "stage", "lastupdated",
                 "closeddate", "reviewedandsigneddate")


def _staff_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", value):
        raise AthenaSchemaError()
    return value


def _staff_response_id(value):
    # Athena emits JSON integers for encounter IDs and related identifiers.
    # Only response decoding accepts these; request IDs stay canonical strings.
    return _staff_id(str(value) if type(value) is int else value)


def _staff_scope(scope):
    if type(scope) is not AthenaStaffScope:
        raise AthenaSchemaError()
    try:
        scope.__post_init__()
    except AthenaStaffScopeError:
        raise AthenaSchemaError() from None
    if (scope.profile.environment == "preview"
            and scope.profile.vendor_practice_id != ATHENA_PREVIEW_PRACTICE_ID):
        raise AthenaSchemaError()
    return scope


def _staff_practice(scope):
    return _staff_scope(scope).profile.vendor_practice_id if scope is not None else ATHENA_PREVIEW_PRACTICE_ID


def _staff_origin(scope):
    return _STAFF_ORIGINS[_staff_scope(scope).profile.environment] if scope is not None else ATHENA_PREVIEW_ORIGIN


def _staff_patient(value, *, scope=None):
    if scope is not None:
        _staff_scope(scope)
        if value != scope.source_patient_id:
            raise AthenaSchemaError()
    if scope is None or scope.profile.environment == "preview":
        if value not in tuple(str(i) for i in range(60178, 60185)):
            raise AthenaSchemaError()
    else:
        _staff_id(value)
    return value


def _staff_target(kind, patient, *, department=None, encounter=None, offset=0, scope=None):
    """Literal GET contract: no caller URL, arbitrary query, or write-like flags."""
    _staff_patient(patient, scope=scope)
    if (type(offset) is not int or offset < 0 or offset % 5
            or offset > (999_999_995 if kind == "encounters" else 10)):
        raise AthenaSchemaError()
    prefix = f"/v1/{_staff_practice(scope)}"
    if kind == "patient":
        return prefix + f"/patients/{patient}", {"showfullssn": "false"}
    if kind == "insurance":
        return prefix + f"/patients/{patient}/insurances", {
            "limit": "5", "offset": str(offset), "showcancelled": "true", "showfullssn": "false"}
    if kind == "encounters":
        return prefix + f"/chart/{patient}/encounters", {
            "departmentid": _staff_id(department), "limit": "5", "offset": str(offset)}
    if kind in ("encounter", "summary"):
        _staff_id(encounter)
        return (prefix + f"/chart/encounter/{encounter}" if kind == "encounter"
                else prefix + f"/chart/encounters/{encounter}/summary"), {}
    raise AthenaOperationUnavailable()


def _staff_url(kind, patient, *, scope=None, **params):
    path, query = _staff_target(kind, patient, scope=scope, **params)
    return _staff_origin(scope) + path + ("?" + urllib.parse.urlencode(query) if query else "")


def build_athena_staff_read_url(scope: AthenaStaffScope, kind: str, *,
                                department=None, encounter=None, offset=0) -> str:
    """Pure fixed-endpoint contract, not authority to send a request.

    The server supplies the immutable case/patient binding. Production URL
    construction does not supply the active lease required by its transport.
    Encounter IDs still require patient-bound discovery before any later read.
    """
    _staff_scope(scope)
    return _staff_url(kind, scope.source_patient_id, scope=scope,
                      department=department, encounter=encounter, offset=offset)


class AthenaStaffReadClient:
    """Executable fixed-origin TLS read lane; construction never performs I/O.

    Legacy Preview is limited to the public synthetic cohort. Production needs
    an exact server scope and active transaction-lived read/storage lease.
    No environment access, credential fallback, introspection, EHR
    write, redirect, proxy, next-link execution, or background activity exists.
    All HTTP (including OAuth) is injectable for offline protocol tests.
    """
    mode = "preview"

    def __init__(self, *, client_id="", client_secret="", token_provider=None,
                 urlopen=None, clock=time.time, monotonic=time.monotonic, max_pages=3,
                 scope: AthenaStaffScope | None = None, credential_environment=None, read_lease=None):
        if scope is not None:
            _staff_scope(scope)
            _staff_patient(scope.source_patient_id, scope=scope)
            if credential_environment != scope.profile.environment:
                raise AthenaAuthenticationError()
            if scope.profile.environment == "production":
                from .athena_admission import AthenaReadLease, AthenaAdmissionError
                if type(read_lease) is not AthenaReadLease: raise AthenaOperationUnavailable()
                try: read_lease.assert_active(scope)
                except AthenaAdmissionError: raise AthenaOperationUnavailable() from None
        elif credential_environment not in (None, "preview"):
            raise AthenaAuthenticationError()
        self._scope = scope
        self._scope_fingerprint = scope.fingerprint if scope is not None else None
        self._credential_environment = credential_environment
        self._read_lease = read_lease
        self.mode = scope.profile.environment if scope is not None else type(self).mode
        if type(max_pages) is not int or not 1 <= max_pages <= 3:
            raise AthenaSchemaError()
        if token_provider is None:
            for value in (client_id, client_secret):
                _safe_string(value, maximum=4096)
            if ":" in client_id:
                raise AthenaAuthenticationError()
        elif not callable(token_provider):
            raise AthenaAuthenticationError()
        self._credentials = (client_id, client_secret)
        self._provider, self._clock, self._monotonic = token_provider, clock, monotonic
        self._max_pages, self._token, self._token_lock = max_pages, None, RLock()
        self._open = urlopen or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context())).open

    def __repr__(self):
        return f"AthenaStaffReadClient(mode={self.mode!r}, credentials='<redacted>')"

    def _assert_transport_scope(self):
        if self._scope is None:
            if self._scope_fingerprint is not None:
                raise AthenaSchemaError()
            environment = "preview"
        else:
            _staff_scope(self._scope)
            if self._scope.fingerprint != self._scope_fingerprint:
                raise AthenaSchemaError()
            environment = self._scope.profile.environment
        if self._credential_environment not in ((None, "preview") if self._scope is None else (environment,)):
            raise AthenaAuthenticationError()
        if environment == "production":
            from .athena_admission import AthenaReadLease, AthenaAdmissionError
            if type(self._read_lease) is not AthenaReadLease: raise AthenaOperationUnavailable()
            try: self._read_lease.assert_active(self._scope)
            except AthenaAdmissionError: raise AthenaOperationUnavailable() from None

    def _wire(self, request, deadline):
        self._assert_transport_scope()
        target, allowed = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(_staff_origin(self._scope))
        if (target.scheme, target.netloc) != (allowed.scheme, allowed.netloc):
            raise AthenaOperationUnavailable()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise AthenaTimeoutError()
        try:
            with self._open(request, timeout=min(5.0, remaining)) as response:
                if response.geturl() != request.full_url or response.status != 200:
                    raise AthenaRequestRejected()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise AthenaSchemaError()
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise AthenaSchemaError()
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,7}", length)
                                           or int(length) > 262144):
                    raise AthenaSchemaError()
                chunks, size = [], 0
                read = getattr(response, "read1", response.read)
                while True:
                    if self._monotonic() >= deadline:
                        raise AthenaTimeoutError()
                    chunk = read(min(8192, 262145 - size))
                    if not isinstance(chunk, bytes):
                        raise AthenaSchemaError()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 262144:
                        raise AthenaSchemaError()
                    chunks.append(chunk)
                if self._monotonic() >= deadline:
                    raise AthenaTimeoutError()
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self._token = None
                raise AthenaAuthenticationError() from None
            if exc.code in (408, 429, 500, 502, 503, 504):
                raise AthenaRetryExhausted() from None
            raise AthenaRequestRejected() from None
        except (socket.timeout, TimeoutError):
            raise AthenaTimeoutError() from None
        except (urllib.error.URLError, OSError):
            raise AthenaRequestRejected() from None

    def _bearer(self, deadline):
        self._assert_transport_scope()
        with self._token_lock:
            now = _validated_now(self._clock())
            if self._token is None or self._token.expires_at <= now + 5:
                if self._provider:
                    token = self._provider()
                else:
                    basic = base64.b64encode(":".join(self._credentials).encode()).decode("ascii")
                    request = urllib.request.Request(_staff_origin(self._scope) + "/oauth2/v1/token", method="POST",
                        headers={"Authorization": "Basic " + basic, "Accept": "application/json",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": ATHENA_PREVIEW_SCOPE}).encode())
                    payload = _card_json(self._wire(request, deadline), text_limit=MAX_TOKEN_CHARS)
                    if not isinstance(payload, dict):
                        raise AthenaAuthenticationError()
                    expiry = payload.get("expires_in")
                    if isinstance(expiry, str) and re.fullmatch(r"[1-9][0-9]{0,3}", expiry):
                        payload["expires_in"] = int(expiry)
                    payload.setdefault("scope", ATHENA_PREVIEW_SCOPE)
                    token = validate_athena_token_response(payload, requested_scope=ATHENA_PREVIEW_SCOPE, now=now)
                if (type(token) is not AthenaAccessToken or token.scope != ATHENA_PREVIEW_SCOPE
                        or not isinstance(token.access_token, str)
                        or any(ord(c) < 33 or ord(c) > 126 for c in token.access_token)
                        or type(token.expires_at) not in (int, float)
                        or not 5 < token.expires_at - now <= 3600):
                    raise AthenaAuthenticationError()
                validate_athena_token_response({"access_token": token.access_token, "token_type": "Bearer",
                    "scope": token.scope, "expires_in": min(3600, int(token.expires_at - now))},
                    requested_scope=ATHENA_PREVIEW_SCOPE, now=now)
                self._token = token
            return self._token.access_token

    def _get(self, kind, patient, deadline, **params):
        self._assert_transport_scope()
        url = _staff_url(kind, patient, scope=self._scope, **params)  # Validate before any token action.
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": "Bearer " + self._bearer(deadline), "Accept": "application/json",
            "Accept-Encoding": "identity"})
        return self._wire(request, deadline)

    def _pages(self, kind, patient, deadline, department=None, *, start_offset=0, page_limit=3, metadata=None):
        pages, records, total, complete, error = [], [], None, False, None
        if metadata is not None:
            metadata.update(pages=[], totalCount=None, nextOffset=None)
        for index in range(min(self._max_pages, page_limit)):
            try:
                offset = start_offset + index * 5
                raw = self._get(kind, patient, deadline, department=department, offset=offset)
                payload = _card_json(raw)
                key = "insurances" if kind == "insurance" else "encounters"
                rows = payload.get(key) if isinstance(payload, dict) else None
                count = payload.get("totalcount") if isinstance(payload, dict) else None
                if (not isinstance(rows, list) or len(rows) > 5
                        or (count is not None and (type(count) is not int or not len(rows) <= count <= 1000000))
                        or (total is not None and count != total)):
                    raise AthenaSchemaError()
                if kind == "insurance":
                    decode_athena_card_read(raw, dataset="insurance", expected_patient_id=patient, scope=self._scope)
                else:
                    for row in rows:
                        _encounter_row(row, patient, department, scope=self._scope)
                for link_name in ("next", "previous"):
                    link = payload.get(link_name)
                    if link is not None and (not isinstance(link, str) or len(link) > 2048):
                        raise AthenaSchemaError()
                next_link = payload.get("next")
                if next_link:
                    # Observe the exact next offset, but construct every request
                    # from this fixed manifest rather than executing the source URL.
                    path, query = _staff_target(kind, patient, department=department, offset=offset, scope=self._scope)
                    query["offset"] = str(offset + 5)
                    expected = _staff_origin(self._scope) + path + "?" + urllib.parse.urlencode(query)
                    if not isinstance(next_link, str) or len(next_link) > 2048:
                        raise AthenaSchemaError()
                    actual = urllib.parse.urlsplit(urllib.parse.urljoin(_staff_origin(self._scope), next_link))
                    wanted = urllib.parse.urlsplit(expected)
                    try:
                        pairs = urllib.parse.parse_qsl(actual.query, strict_parsing=True)
                        expected_pairs = urllib.parse.parse_qsl(wanted.query, strict_parsing=True)
                    except ValueError:
                        raise AthenaSchemaError() from None
                    # Preview continuation links change the casing of flag
                    # names/booleans. Compare their meaning, then construct our
                    # next GET from the fixed manifest; never execute the link.
                    pairs = [(name.lower(), value.lower() if name.lower() in
                              {"showcancelled", "showfullssn"} else value)
                             for name, value in pairs]
                    # A final continuation may advertise only the remaining
                    # records. Our request still uses the fixed five-row cap.
                    remaining = count - start_offset - len(records) - len(rows) if count is not None else None
                    if remaining is not None and 0 < remaining < 5:
                        pairs = [(name, "5" if name == "limit" and value == str(remaining) else value)
                                 for name, value in pairs]
                    if ((actual.scheme, actual.netloc, actual.path, actual.fragment)
                            != (wanted.scheme, wanted.netloc, wanted.path, "")
                            or len(pairs) != len(dict(pairs)) or sorted(pairs) != sorted(expected_pairs)):
                        raise AthenaSchemaError()
                received = start_offset + len(records) + len(rows)
                if count is not None and received > count:
                    raise AthenaSchemaError()
                if next_link and (len(rows) != 5 or (count is not None and count <= received)):
                    raise AthenaSchemaError()
                pages.append(raw)
                records.extend(rows)
                total = count
                if metadata is not None:
                    metadata["pages"].append({"offset": offset, "sha256": hashlib.sha256(raw).hexdigest(),
                        "encounterIds": [_staff_response_id(row["encounterid"]) for row in rows]})
                    metadata.update(totalCount=count, nextOffset=offset + 5 if next_link else None)
                if not next_link:
                    complete = count == start_offset + len(records)
                    break
            except AthenaClientError as exc:
                error = exc.code
                break
        return pages, records, complete, error

    def read_card(self, patient):
        _staff_patient(patient, scope=self._scope)
        deadline = self._monotonic() + 25
        raw = self._get("patient", patient, deadline)
        decode_athena_card_read(raw, dataset="patient", expected_patient_id=patient, scope=self._scope)
        pages, records, complete, error = self._pages("insurance", patient, deadline)
        state = "complete" if complete and records else "empty" if complete else "partial" if pages else "unavailable"
        return {"patient": raw, "insurancePages": pages, "insuranceState": state, "error": error}

    def read_billing(self, patient, department, encounter=None, *, boundary=None, page=None, expected_total=None):
        _staff_patient(patient, scope=self._scope)
        _staff_id(department)
        if encounter is not None:
            _staff_id(encounter)
        if boundary is not None and (page is not None or encounter is not None):
            raise AthenaSchemaError()
        if page is not None and encounter is None:
            raise AthenaSchemaError()
        receipt = boundary if boundary is not None else page
        if receipt is not None:
            _staff_page_receipt(receipt)
            if (expected_total is not None and (type(expected_total) is not int
                    or not 0 <= expected_total <= 1_000_000)):
                raise AthenaSchemaError()
        deadline = self._monotonic() + 25
        start_offset, page_limit = 0, 3
        if boundary is not None:
            check = {}
            before, _, _, failure = self._pages("encounters", patient, deadline, department,
                start_offset=boundary["offset"], page_limit=1, metadata=check)
            if failure:
                raise AthenaClientError()
            if (len(before) != 1 or check["pages"] != [boundary]
                    or check["totalCount"] != expected_total
                    or check["nextOffset"] != boundary["offset"] + 5):
                raise AthenaPartialResponseError()
            start_offset, page_limit = boundary["offset"] + 5, 2
        elif page is not None:
            start_offset, page_limit = page["offset"], 1
        pagination = {}
        pages, rows, complete, error = self._pages("encounters", patient, deadline, department,
            start_offset=start_offset, page_limit=page_limit, metadata=pagination)
        if receipt is not None:
            if error:
                raise AthenaClientError()
            if pagination["totalCount"] != expected_total:
                raise AthenaPartialResponseError()
            if page is not None and (error or pagination["pages"] != [page]):
                raise AthenaPartialResponseError()
        ids = [_staff_response_id(row["encounterid"]) for row in rows]
        if len(ids) != len(set(ids)):
            raise AthenaSchemaError()
        result = {"schemaVersion": "athena-billing-read.v1", "sourcePatientId": patient,
            "departmentId": department, "departmentIdMeaning": "chart_query", "encounters": [_encounter_row(row, patient, department, scope=self._scope) for row in rows],
            "state": "complete" if complete and rows else "empty" if complete else "partial" if pages else "unavailable",
            "pageHashes": [hashlib.sha256(page).hexdigest() for page in pages], "error": error,
            "pagination": pagination, "note": None, "unknownFields": ["place_of_service", "patient_age", "patient_sex",
                "provider_specialty", "submitted_codes", "eligible_em_time"], "readyForHeader": False}
        if encounter is None:
            return result
        if encounter not in ids:
            raise AthenaSchemaError()
        before = self._get("encounter", patient, deadline, encounter=encounter)
        metadata_payload = _card_json(before)
        # The exact-encounter endpoint also returns a singleton record array.
        # Require one record and retain the same identity checks and raw hash.
        if isinstance(metadata_payload, list):
            if len(metadata_payload) != 1:
                raise AthenaSchemaError()
            metadata_payload = metadata_payload[0]
        metadata = _encounter_row(metadata_payload, patient, department, encounter, scope=self._scope)
        # The query department is chart access context, not an encounter filter.
        # Bind a known list observation to the exact metadata before reading text.
        selected = next(row for row in result["encounters"] if row["encounterid"]["value"] == encounter)
        listed_department, actual_department = selected["departmentid"], metadata["departmentid"]
        if listed_department["state"] == "present" and actual_department != listed_department:
            raise AthenaPartialResponseError()
        source_department = actual_department["value"]
        department_origin = ("list_and_metadata" if listed_department["state"] == "present"
                             else "metadata" if actual_department["state"] == "present" else "not_supplied")
        raw = self._get("summary", patient, deadline, encounter=encounter)
        payload = _card_json(raw, text_limit=131072, allow_lines=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("summaryhtml"), str):
            raise AthenaSchemaError()
        for key, expected in (("patientid", patient), ("encounterid", encounter),
                              ("practiceid", _staff_practice(self._scope))):
            if key in payload and _staff_response_id(payload[key]) != expected:
                raise AthenaSchemaError()
        html = payload["summaryhtml"]
        extractor = _SummaryText()
        try:
            extractor.feed(html)
            extractor.close()
        except (ValueError, RecursionError):
            raise AthenaSchemaError() from None
        after = self._get("encounter", patient, deadline, encounter=encounter)
        if before != after:
            raise AthenaPartialResponseError()
        # The endpoint offers no atomic summary+metadata version guarantee.
        # Retain both hashes and require explicit provider source confirmation.
        result["note"] = {"encounterId": encounter, "metadata": metadata,
            "sourceDepartmentId": source_department, "sourceDepartmentOrigin": department_origin,
            "metadataSha256": hashlib.sha256(before).hexdigest(),
            "responseSha256": hashlib.sha256(raw).hexdigest(), "sourcePointer": "/summaryhtml",
            "summaryHtml": html, "plainText": "".join(extractor.parts).strip(),
            "transformVersion": "athena-summary-text.v1", "offsetUnit": "unicode_code_point",
            "sourceAtomicity": "unverified", "signatureInterpretation": "unverified",
            "requiresProviderConfirmation": True}
        return result



def _staff_page_receipt(value):
    """Validated service receipt, never a URL or authority to select a patient."""
    if (not isinstance(value, dict) or set(value) != {"offset", "sha256", "encounterIds"}
            or type(value["offset"]) is not int or not 0 <= value["offset"] <= 999_999_995
            or value["offset"] % 5 or not isinstance(value["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
            or not isinstance(value["encounterIds"], list) or not 0 <= len(value["encounterIds"]) <= 5):
        raise AthenaSchemaError()
    for identifier in value["encounterIds"]:
        _staff_id(identifier)
    if len(set(value["encounterIds"])) != len(value["encounterIds"]):
        raise AthenaSchemaError()


def _encounter_row(row, patient, _query_department, encounter=None, *, scope=None):
    if scope is not None:
        _staff_patient(patient, scope=scope)
    if not isinstance(row, dict):
        raise AthenaSchemaError()
    identifier = _staff_response_id(row.get("encounterid"))
    if encounter is not None and identifier != encounter:
        raise AthenaSchemaError()
    for key, expected in (("patientid", patient), ("practiceid", _staff_practice(scope))):
        if key in row and _staff_response_id(row[key]) != expected:
            raise AthenaSchemaError()
    # Response departmentid describes this encounter, not the query context.
    # Missing remains unknown; present null/empty/malformed IDs remain rejected.
    if "departmentid" in row:
        _staff_response_id(row["departmentid"])
    row = dict(row)  # Retain the original response bytes for provenance.
    for key in ("encounterid", "appointmentid", "departmentid", "providerid"):
        if key in row and type(row[key]) is int:
            row[key] = _staff_response_id(row[key])
    return {key: {"state": "absent" if key not in row else "null" if row[key] is None
                         else "present" if isinstance(row[key], str) else "invalid",
                  "value": row.get(key) if isinstance(row.get(key), str) else None}
            for key in _STAFF_FIELDS}


class _SummaryText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "template"):
            self.skip += 1
        if not self.skip and tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "template") and self.skip:
            self.skip -= 1
        if not self.skip and tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class MockAthenaStaffReadClient(AthenaStaffReadClient):
    """Invented wire responses exercise the same real decoder and transport."""
    mode = "mock"

    def __init__(self):
        import io

        def open_mock(request, timeout):
            url = urllib.parse.urlsplit(request.full_url)
            path, query = url.path, urllib.parse.parse_qs(url.query)
            match = re.search(r"/(6017[8-9]|6018[0-4])(?:/|$)", path)
            source_encounter = re.search(r"/encounters?/(8800[1-7])(?:/|$)", path)
            patient = (match.group(1) if match else str(60177 + int(source_encounter.group(1)[-1]))
                       if source_encounter else "60178")
            metadata = {"encounterid": "8800" + str(int(patient) - 60177), "patientid": patient, "departmentid": "1",
                "status": "CLOSED", "lastupdated": "09/29/2026", "reviewedandsigneddate": "09/29/2026"}
            if path.endswith("/insurances"):
                payload = {"totalcount": 1, "insurances": [{"insurancepackageid": "44001",
                    "insuranceid": "55001", "sequencenumber": "1", "insuranceidnumber": "DEMO-MEMBER-42",
                    "policynumber": "DEMO-GROUP-7", "insuranceplanname": "Invented Harbor Plan",
                    "insurancepolicyholderfirstname": "Casey", "insurancepolicyholderlastname": "Example",
                    "relationshiptoinsured": "Self"}]}
            elif path.endswith("/encounters"):
                payload = {"totalcount": 1, "encounters": [metadata]}
            elif path.endswith("/summary"):
                from html import escape
                from .corpus.demo import SAMPLE_VISIT
                # Explicit local fixture only: keep every existing mock evidence
                # span in the source, visibly separated by original section.
                payload = {"summaryhtml": "<h1>Bundled synthetic visit</h1>" + "".join(
                    "<h2>" + heading + "</h2><div>" + escape(value) + "</div>"
                    for heading, value in (("Intake", SAMPLE_VISIT.intake_text),
                        ("Transcript", SAMPLE_VISIT.transcript), ("Signed note", SAMPLE_VISIT.signed_note)))}
            elif "/chart/encounter/" in path:
                payload = metadata
            else:
                payload = [{"patientid": patient, "firstname": "Casey", "lastname": "Example",
                    "dob": "01/02/2000", "departmentid": "1", "mobilephone": "+12025550142",
                    "email": "casey@example.invalid", "consenttotext": "unknown"}]
            response = io.BytesIO(json.dumps(payload).encode())
            response.status, response.headers = 200, {"Content-Type": "application/json"}
            response.geturl = lambda: request.full_url
            return response
        super().__init__(token_provider=lambda: AthenaAccessToken("synthetic-token", ATHENA_PREVIEW_SCOPE,
            time.time() + 300), urlopen=open_mock)
