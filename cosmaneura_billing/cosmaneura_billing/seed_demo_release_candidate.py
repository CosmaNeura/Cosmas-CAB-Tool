"""Seed the one approved synthetic release-candidate encounter.

This command intentionally has no input-file or payload option.  It loads the
validated package artifact, selects the single approved case, and writes it to
the configured PostgreSQL practice.  An exact retry is a no-op; an existing
row with any different persisted value is a hard conflict and is never
updated.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from .corpus.demo_release_candidate import load_demo_release_candidate
from .persistence import PostgresContext
from .settings import Settings


PACK_ID = "cosmaneura-demo-rc-v1"
CASE_ID = "SYN-RC-0001"
SOURCE = "synthetic_demo_release_candidate"
EXPECTED_DIGEST = "c847409a34bc382634eea22a0de7c611d99effc73079c31b29fc6e9c5b970879"


class SyntheticDemoSeedError(RuntimeError):
    """The fixed synthetic seed could not be applied safely."""


class SyntheticDemoPracticeBindingError(SyntheticDemoSeedError):
    """The configured PostgreSQL practice binding was not proven."""


class SyntheticDemoConflict(SyntheticDemoSeedError):
    """The approved case ID is already occupied by different content."""


@dataclass(frozen=True)
class PreparedSyntheticDemoSeed:
    """Validated, deterministic input for the one permitted seed operation."""

    visit: dict[str, object]
    digest: str


def prepare_synthetic_demo_seed() -> PreparedSyntheticDemoSeed:
    """Load and digest only the bundled, validated ``SYN-RC-0001`` case."""
    pack = load_demo_release_candidate()
    if pack.get("pack_id") != PACK_ID:
        raise SyntheticDemoSeedError("the bundled synthetic pack is not approved")
    matches = [case for case in pack["cases"] if case.get("case_id") == CASE_ID]
    if len(matches) != 1:
        raise SyntheticDemoSeedError("the approved synthetic case is unavailable")
    case = matches[0]
    visit = dict(case["visit"])
    if visit.get("encounter_id") != CASE_ID:
        raise SyntheticDemoSeedError("the approved synthetic case is malformed")
    digest_payload = {
        "case_id": CASE_ID,
        "pack_id": PACK_ID,
        "source": SOURCE,
        "visit": visit,
    }
    canonical = json.dumps(
        digest_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != EXPECTED_DIGEST:
        raise SyntheticDemoSeedError("the approved synthetic case digest changed")
    return PreparedSyntheticDemoSeed(visit=visit, digest=digest)


def _validate_postgres_binding_settings(settings: Settings) -> str:
    # This command has no local-store implementation: a DSN is the only route
    # to a store, and the practice/tenant checks below must succeed inside that
    # PostgreSQL transaction before an encounter statement is attempted.
    if not settings.database_url.strip():
        raise SyntheticDemoPracticeBindingError(
            "the synthetic seed requires PostgreSQL state"
        )
    practice_id = settings.practice_id
    if (
        not practice_id
        or practice_id != practice_id.strip()
        or len(practice_id) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in practice_id)
    ):
        raise SyntheticDemoPracticeBindingError(
            "the configured practice binding is invalid"
        )
    try:
        tenant_id = str(UUID(settings.entra_tenant_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SyntheticDemoPracticeBindingError(
            "the configured practice binding is invalid"
        ) from exc
    if tenant_id != settings.entra_tenant_id.casefold():
        raise SyntheticDemoPracticeBindingError(
            "the configured practice binding is invalid"
        )
    return tenant_id


def _expected_persisted_values(
    prepared: PreparedSyntheticDemoSeed,
) -> dict[str, object]:
    visit = prepared.visit
    return {
        "external_id": CASE_ID,
        "patient_age": visit["patient_age"],
        "patient_sex": visit["patient_sex"],
        "encounter_type": visit["encounter_type"],
        "place_of_service": visit["place_of_service"],
        "intake_text": visit["intake_text"],
        "transcript": visit["transcript"],
        "signed_note": visit["signed_note"],
        "provider_specialty": visit["provider_specialty"],
        "submitted_em_code": visit["submitted_em_code"],
        "submitted_icd10": list(visit["submitted_icd10"]),
        "source": SOURCE,
        "ingested_by_tenant_id": None,
        "ingested_by_user_id": None,
        "version": 1,
    }


def _row_is_exact_replay(
    row: dict[str, object] | None,
    expected: dict[str, object],
) -> bool:
    if row is None:
        return False
    normalized = dict(row)
    submitted = normalized.get("submitted_icd10")
    if submitted is not None:
        normalized["submitted_icd10"] = list(submitted)  # type: ignore[arg-type]
    return normalized == expected


def seed_synthetic_demo_release_candidate(settings: Settings) -> dict[str, str]:
    """Insert the fixed case, or return a no-op result for an exact replay."""
    tenant_id = _validate_postgres_binding_settings(settings)
    prepared = prepare_synthetic_demo_seed()
    expected = _expected_persisted_values(prepared)
    context = PostgresContext(
        settings.database_url,
        practice_id=settings.practice_id,
    )
    with context.connection() as connection:
        binding = connection.execute(
            """SELECT p.practice_id,
                      p.entra_tenant_id::text AS tenant_id,
                      p.active,
                      current_setting('cosmaneura.practice_id', true)
                          AS selected_practice_id,
                      public.cosmaneura_practice_allowed(p.practice_id)
                          AS practice_allowed
                 FROM practices AS p
                WHERE p.practice_id = %s
                  AND p.entra_tenant_id = %s::uuid
                  AND p.active
                FOR SHARE""",
            (settings.practice_id, tenant_id),
        ).fetchone()
        if (
            binding is None
            or binding.get("practice_id") != settings.practice_id
            or binding.get("tenant_id") != tenant_id
            or binding.get("active") is not True
            or binding.get("selected_practice_id") != settings.practice_id
            or binding.get("practice_allowed") is not True
        ):
            raise SyntheticDemoPracticeBindingError(
                "the configured practice binding was not proven"
            )

        visit = prepared.visit
        inserted = connection.execute(
            """INSERT INTO encounters (
                   practice_id, external_id, patient_age, patient_sex,
                   encounter_type, place_of_service, intake_text, transcript,
                   signed_note, provider_specialty, submitted_em_code,
                   submitted_icd10, source
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
               )
               ON CONFLICT (practice_id, external_id) DO NOTHING
               RETURNING external_id""",
            (
                settings.practice_id,
                CASE_ID,
                visit["patient_age"],
                visit["patient_sex"],
                visit["encounter_type"],
                visit["place_of_service"],
                visit["intake_text"],
                visit["transcript"],
                visit["signed_note"],
                visit["provider_specialty"],
                visit["submitted_em_code"],
                list(visit["submitted_icd10"]),  # type: ignore[arg-type]
                SOURCE,
            ),
        ).fetchone()
        stored = connection.execute(
            """SELECT external_id, patient_age, patient_sex, encounter_type,
                      place_of_service, intake_text, transcript, signed_note,
                      provider_specialty, submitted_em_code, submitted_icd10,
                      source, ingested_by_tenant_id, ingested_by_user_id, version
                 FROM encounters
                WHERE practice_id = %s AND external_id = %s
                FOR SHARE""",
            (settings.practice_id, CASE_ID),
        ).fetchone()
        if not _row_is_exact_replay(stored, expected):
            raise SyntheticDemoConflict(
                "the approved synthetic case ID has conflicting content"
            )

    return {
        "pack_id": PACK_ID,
        "case_id": CASE_ID,
        "status": "inserted" if inserted is not None else "replayed",
        "digest": prepared.digest,
    }


def _closed_result(*, status: str, digest: str) -> dict[str, str]:
    return {
        "pack_id": PACK_ID,
        "case_id": CASE_ID,
        "status": status,
        "digest": digest,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixed seed without accepting or reflecting caller input."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    digest = EXPECTED_DIGEST
    try:
        prepared = prepare_synthetic_demo_seed()
        digest = prepared.digest
        if arguments:
            result = _closed_result(status="arguments_rejected", digest=digest)
            print(json.dumps(result, sort_keys=True))
            return 2
        result = seed_synthetic_demo_release_candidate(Settings.from_env())
    except SyntheticDemoConflict:
        result = _closed_result(status="conflict", digest=digest)
        exit_code = 1
    except SyntheticDemoPracticeBindingError:
        result = _closed_result(status="binding_rejected", digest=digest)
        exit_code = 1
    except Exception:
        # Never emit exception text, a DSN, or encounter content.  Operators get
        # only the fixed artifact identity, digest, and a closed status token.
        result = _closed_result(status="failed", digest=digest)
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
