"""Mock-first boundary for the future Athena sandbox read integration.

This module intentionally does not claim to know Athena's endpoint paths,
OAuth exchange, scopes, or response schema.  Those details must come from the
approved sandbox contract.  The boundary is split into three independently
testable pieces:

* :class:`MockAthenaClient` supplies deterministic pages made only from the
  repository's wholly synthetic release-candidate pack;
* :class:`AthenaHTTPTransport` is a bounded stdlib HTTP transport with an
  injected bearer-token provider; and
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
from typing import Any, Callable, Mapping, Protocol

from .corpus.demo_release_candidate import load_demo_release_candidate
from .schemas import VisitBundle


NORMALIZED_SCHEMA_VERSION = "cosmaneura-athena-normalized-v1"
MAX_PAGE_RECORDS = 100
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 65_536
MAX_CURSOR_CHARS = 512

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


def _safe_string(value: Any, *, minimum: int = 1, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
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


def _idempotency_key(source_record_id: str, source_version: str) -> str:
    canonical = json.dumps(
        [source_record_id, source_version],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "athena-read-v1:" + hashlib.sha256(canonical).hexdigest()


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
        next_cursor = _safe_string(next_cursor, maximum=MAX_CURSOR_CHARS)
    checkpoint = _safe_string(page["checkpoint"], maximum=MAX_CURSOR_CHARS)

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
            idempotency_key=_idempotency_key(
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
        _safe_string(starting_cursor, maximum=MAX_CURSOR_CHARS)

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
            _safe_string(cursor, maximum=MAX_CURSOR_CHARS)
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


class AthenaHTTPTransport:
    """Bounded stdlib JSON transport; it knows no Athena resource paths."""

    def __init__(
        self,
        *,
        base_url: str,
        token_provider: Callable[[], str],
        max_retries: int = 2,
        timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = validate_athena_https_url(base_url).rstrip("/")
        if not callable(token_provider):
            raise AthenaAuthenticationError() from None
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
        self._max_retries = max_retries
        self._timeout = float(timeout)
        self._max_response_bytes = max_response_bytes
        self._urlopen = urlopen or urllib.request.build_opener(
            _NoRedirectHandler(),
        ).open
        self._sleep = sleep or time.sleep

    def _bearer(self) -> str:
        try:
            token = self._token_provider()
        except Exception:
            raise AthenaAuthenticationError() from None
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 16_384
            or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            raise AthenaAuthenticationError() from None
        return token

    def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = validate_athena_https_url(url, base_url=self.base_url)
        if method not in {"GET", "POST"}:
            raise AthenaSchemaError() from None
        if method == "GET" and body is not None:
            raise AthenaSchemaError() from None

        encoded: bytes | None = None
        if body is not None:
            if not isinstance(body, Mapping):
                raise AthenaSchemaError() from None
            try:
                encoded = json.dumps(
                    dict(body),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise AthenaSchemaError() from None
            if len(encoded) > MAX_REQUEST_BYTES:
                raise AthenaSchemaError() from None

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._bearer()}",
            "User-Agent": "cosmaneura-athena-sandbox/1",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            target,
            data=encoded,
            headers=headers,
            method=method,
        )

        saw_timeout = False
        for attempt in range(self._max_retries + 1):
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


@dataclass(frozen=True, repr=False)
class AthenaRequest:
    """A reviewed contract adapter's request; representation is redacted."""

    url: str
    method: str = "GET"
    body: Mapping[str, Any] | None = None

    def __repr__(self) -> str:
        return "AthenaRequest(<redacted>)"


class AthenaContract(Protocol):
    """Vendor-specific behavior to implement only after contract approval."""

    def build_page_request(self, cursor: str | None) -> AthenaRequest:
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
        contract: AthenaContract,
        server_practice_id: str,
    ) -> None:
        if not isinstance(transport, AthenaHTTPTransport):
            raise AthenaSchemaError() from None
        self._transport = transport
        self._contract = contract
        self._server_practice_id = _safe_string(
            server_practice_id,
            maximum=256,
        )

    def read_page(self, cursor: str | None = None) -> AthenaPage:
        if cursor is not None:
            _safe_string(cursor, maximum=MAX_CURSOR_CHARS)
        try:
            request = self._contract.build_page_request(cursor)
        except AthenaClientError:
            raise
        except Exception:
            raise AthenaSchemaError() from None
        if not isinstance(request, AthenaRequest):
            raise AthenaSchemaError() from None

        payload = self._transport.request_json(
            request.url,
            method=request.method,
            body=request.body,
        )
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
