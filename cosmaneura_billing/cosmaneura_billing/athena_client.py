"""Mock-first boundary for the future Athena sandbox read integration.

This module pins only the Preview origin, OAuth metadata locations, public
synthetic practice, and portal-selected scope verified during access review.
It intentionally does not claim to know Athena's proprietary resource paths,
query parameters, OAuth wire exchange, or response schema.  Those details
must come from the approved sandbox contract.  The boundary is split into
three independently testable pieces:

* :class:`MockAthenaClient` supplies deterministic pages made only from the
  repository's wholly synthetic release-candidate pack;
* :class:`AthenaHTTPTransport` is a bounded, GET-only stdlib transport behind
  an empty-until-confirmed named-operation allowlist; and
* :class:`AthenaSandboxClient` composes the transport with an explicitly
  supplied contract adapter once the real contract has been reviewed.

No factory silently selects the live transport.  Constructing the mock never
reads credentials or opens a socket, and the one-shot sync skeleton runs only
the mock in this release.
"""
from __future__ import annotations

import hashlib
import json
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


NORMALIZED_SCHEMA_VERSION = "cosmaneura-athena-normalized-v1"
ATHENA_PREVIEW_ORIGIN = "https://api.preview.platform.athenahealth.com"
ATHENA_PREVIEW_TOKEN_URL = ATHENA_PREVIEW_ORIGIN + "/oauth2/v1/token"
ATHENA_PREVIEW_INTROSPECTION_URL = (
    ATHENA_PREVIEW_ORIGIN + "/oauth2/v1/introspect"
)
ATHENA_PREVIEW_PRACTICE_ID = "195900"
ATHENA_PREVIEW_SCOPE = "athena/service/Athenanet.MDP.*"
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
