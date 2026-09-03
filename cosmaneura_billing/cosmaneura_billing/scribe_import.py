"""Receiver-first ingestion adapter for de-identified visit bundles.

A producer (Scribe first; any de-identified source second) hands CAB a bounded
page of finished visits shaped by the ``scribe.visit.v1`` contract in
``docs/contracts/scribe-visit-ingest/v1``.  This module owns *only* the protocol
concern: validate the external page, normalize each record into a
``VisitBundle``-carrying ``SourceEncounterRecord``, and hand the page to the
existing durable, idempotent :mod:`cosmaneura_billing.source_import` layer, which
owns the replay/checkpoint transaction.

Boundaries this adapter deliberately keeps:

* It performs **no de-identification.**  The producer is contractually
  responsible for emitting synthetic or properly de-identified text.  This
  adapter validates shape and normalizes; it never scrubs, and it never claims
  a PHI gate is satisfied.
* Its provenance (``scribe_deidentified``) is intentionally **not** one of
  :data:`persistence.READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES`, so ingested
  encounters do not masquerade as the fixed synthetic-demo pack and the
  synthetic-only inference latch is unaffected.
* In PostgreSQL the server-owned ``cosmaneura_source_import_allowed(practice_id,
  source_system, provenance)`` binding governs acceptance; this adapter cannot
  widen that gate.
* Errors are fail-closed and identifier-free: a malformed page raises
  :class:`~cosmaneura_billing.source_import.SourceImportSchemaError` with a
  static message and never echoes offending values.

The contract is additive and versioned by ``contract_version`` so a future
``scribe.visit.v2`` can be introduced without breaking v1 producers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .schemas import VisitBundle
from .source_import import (
    SourceImportPageResult,
    SourceImportSchemaError,
    SourceImportStore,
    source_import_external_id,
    source_import_idempotency_key,
)

#: Stable source-system identifier for the Scribe producer.
SCRIBE_SOURCE_SYSTEM = "scribe"
#: The only feed the encounter writer accepts (mirrors source_import).
ENCOUNTER_FEED = "encounters"
#: Server-side provenance recorded for de-identified Scribe ingestion.
SCRIBE_DEIDENTIFIED_PROVENANCE = "scribe_deidentified"
#: Wire contract version; also used as the normalized ``mapping_version``.
INGEST_CONTRACT_VERSION = "scribe.visit.v1"

# Bounds are generous enough for real clinical text but reject absurd input
# before the source_import layer's 16 MiB whole-page ceiling is reached.
MAX_TEXT_FIELD_CHARS = 200_000
MAX_LABEL_CHARS = 64
MAX_SUBMITTED_ICD10 = 64
MIN_PATIENT_AGE = 0
MAX_PATIENT_AGE = 130
MAX_BATCH_ID_CHARS = 256

_REQUIRED_RECORD_KEYS = frozenset({"source_record_id", "source_version", "visit"})
_ALLOWED_RECORD_KEYS = _REQUIRED_RECORD_KEYS
_REQUIRED_VISIT_KEYS = frozenset(
    {"encounter_type", "place_of_service", "patient_age", "patient_sex"}
)
_ALLOWED_VISIT_KEYS = _REQUIRED_VISIT_KEYS | frozenset(
    {
        "intake_text",
        "transcript",
        "signed_note",
        "provider_specialty",
        "submitted_em_code",
        "submitted_icd10",
    }
)
_ALLOWED_PAGE_KEYS = frozenset({"contract_version", "batch_id", "records"})


@dataclass(frozen=True, repr=False)
class ScribeEncounterRecord:
    """Normalized record implementing the ``SourceEncounterRecord`` seam.

    ``repr`` is suppressed so visit text never lands in a traceback or log.
    """

    target_practice_id: str
    source_record_id: str
    source_version: str
    mapping_version: str
    idempotency_key: str
    visit: VisitBundle

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            "ScribeEncounterRecord("
            f"mapping_version={self.mapping_version!r})"
        )


def _require_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceImportSchemaError() from None
    return value


def _bounded_text(value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_FIELD_CHARS:
        raise SourceImportSchemaError() from None
    if not allow_empty and not value.strip():
        raise SourceImportSchemaError() from None
    return value


def _safe_label(value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or len(value) > MAX_LABEL_CHARS:
        raise SourceImportSchemaError() from None
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SourceImportSchemaError() from None
    if not allow_empty and not value:
        raise SourceImportSchemaError() from None
    return value


def _patient_age(value: Any) -> int:
    # ``bool`` is an ``int`` subclass; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceImportSchemaError() from None
    if not MIN_PATIENT_AGE <= value <= MAX_PATIENT_AGE:
        raise SourceImportSchemaError() from None
    return value


def _optional_code(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_label(value, allow_empty=False)


def _submitted_icd10(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SourceImportSchemaError() from None
    if len(value) > MAX_SUBMITTED_ICD10:
        raise SourceImportSchemaError() from None
    return [_safe_label(code, allow_empty=False) for code in value]


def _build_visit(payload: Mapping[str, Any], *, encounter_id: str) -> VisitBundle:
    keys = set(payload)
    if not _REQUIRED_VISIT_KEYS <= keys or not keys <= _ALLOWED_VISIT_KEYS:
        raise SourceImportSchemaError() from None
    return VisitBundle(
        encounter_id=encounter_id,
        patient_age=_patient_age(payload["patient_age"]),
        patient_sex=_safe_label(payload["patient_sex"], allow_empty=False),
        encounter_type=_safe_label(payload["encounter_type"], allow_empty=False),
        place_of_service=_safe_label(payload["place_of_service"], allow_empty=False),
        intake_text=_bounded_text(payload.get("intake_text", ""), allow_empty=True),
        transcript=_bounded_text(payload.get("transcript", ""), allow_empty=True),
        # Billing is derived from the signed note, so it must carry content.
        signed_note=_bounded_text(payload.get("signed_note", ""), allow_empty=False),
        provider_specialty=_safe_label(
            payload.get("provider_specialty", "Family Medicine"),
            allow_empty=False,
        ),
        submitted_em_code=_optional_code(payload.get("submitted_em_code")),
        submitted_icd10=_submitted_icd10(payload.get("submitted_icd10")),
    )


def _normalize_record(
    payload: Mapping[str, Any],
    *,
    practice_id: str,
    source_system: str,
    mapping_version: str,
) -> ScribeEncounterRecord:
    keys = set(payload)
    if not _REQUIRED_RECORD_KEYS <= keys or not keys <= _ALLOWED_RECORD_KEYS:
        raise SourceImportSchemaError() from None
    source_record_id = payload["source_record_id"]
    source_version = payload["source_version"]
    # These helpers validate the source identity strings and raise a
    # SourceImportSchemaError on anything malformed.
    external_id = source_import_external_id(
        practice_id=practice_id,
        source_system=source_system,
        feed=ENCOUNTER_FEED,
        source_record_id=source_record_id,
    )
    idempotency_key = source_import_idempotency_key(
        practice_id=practice_id,
        source_system=source_system,
        feed=ENCOUNTER_FEED,
        source_record_id=source_record_id,
        source_version=source_version,
        mapping_version=mapping_version,
    )
    visit = _build_visit(_require_mapping(payload["visit"]), encounter_id=external_id)
    return ScribeEncounterRecord(
        target_practice_id=practice_id,
        source_record_id=source_record_id,
        source_version=source_version,
        mapping_version=mapping_version,
        idempotency_key=idempotency_key,
        visit=visit,
    )


def normalize_records(
    records: Any,
    *,
    practice_id: str,
    source_system: str = SCRIBE_SOURCE_SYSTEM,
    mapping_version: str = INGEST_CONTRACT_VERSION,
) -> tuple[ScribeEncounterRecord, ...]:
    """Validate and normalize a sequence of wire records.

    Fail-closed on any malformed record; never returns a partial batch.
    """

    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise SourceImportSchemaError() from None
    return tuple(
        _normalize_record(
            _require_mapping(record),
            practice_id=practice_id,
            source_system=source_system,
            mapping_version=mapping_version,
        )
        for record in records
    )


def _validate_batch_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_BATCH_ID_CHARS
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise SourceImportSchemaError() from None
    return value


def ingest_visit_page(
    store: SourceImportStore,
    page: Mapping[str, Any],
    *,
    practice_id: str,
    source_system: str = SCRIBE_SOURCE_SYSTEM,
    provenance: str = SCRIBE_DEIDENTIFIED_PROVENANCE,
    expected_checkpoint_version: int | None = None,
) -> SourceImportPageResult:
    """Validate one wire page and commit it through the durable import store.

    The page is committed with its ``batch_id`` as the checkpoint position, so a
    re-posted identical batch dedupes to a pure replay and never advances state.
    When ``expected_checkpoint_version`` is omitted, the store's current version
    is read and supplied, and the store's optimistic-concurrency check still
    fails closed on a concurrent writer.
    """

    page = _require_mapping(page)
    keys = set(page)
    if not {"contract_version", "batch_id", "records"} <= keys or not keys <= _ALLOWED_PAGE_KEYS:
        raise SourceImportSchemaError() from None
    if page["contract_version"] != INGEST_CONTRACT_VERSION:
        raise SourceImportSchemaError() from None
    batch_id = _validate_batch_id(page["batch_id"])
    records = normalize_records(
        page["records"],
        practice_id=practice_id,
        source_system=source_system,
        mapping_version=INGEST_CONTRACT_VERSION,
    )
    if not records:
        raise SourceImportSchemaError() from None

    if expected_checkpoint_version is None:
        current = store.load_checkpoint(
            source_system=source_system,
            feed=ENCOUNTER_FEED,
        )
        expected_checkpoint_version = current.version if current is not None else None

    return store.import_encounter_page(
        source_system=source_system,
        feed=ENCOUNTER_FEED,
        provenance=provenance,
        mapping_version=INGEST_CONTRACT_VERSION,
        records=records,
        checkpoint=batch_id,
        next_cursor=None,
        expected_checkpoint_version=expected_checkpoint_version,
    )


__all__ = [
    "ENCOUNTER_FEED",
    "INGEST_CONTRACT_VERSION",
    "SCRIBE_DEIDENTIFIED_PROVENANCE",
    "SCRIBE_SOURCE_SYSTEM",
    "ScribeEncounterRecord",
    "ingest_visit_page",
    "normalize_records",
]
