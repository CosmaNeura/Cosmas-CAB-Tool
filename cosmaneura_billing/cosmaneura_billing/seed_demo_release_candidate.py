"""Seed the complete approved synthetic release-candidate pack.

This command intentionally has no input-file or payload option. It loads and
validates the immutable bundled pack before opening a database connection,
then inserts all four approved cases in one transaction. An exact retry is a
no-op; any occupied case ID with different persisted content aborts the whole
transaction without updating or partially inserting the pack.
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
SOURCE = "synthetic_demo_release_candidate"
CASE_IDS = (
    "SYN-RC-0001",
    "SYN-RC-0002",
    "SYN-RC-0003",
    "SYN-RC-0004",
)
EXPECTED_PACK_DIGEST = (
    "556b4913553e7fddc8063f1c6d47b64e1cdec7a9e85ab873fdb81cc4e13be68f"
)
EXPECTED_CASE_DIGESTS = {
    "SYN-RC-0001": "c847409a34bc382634eea22a0de7c611d99effc73079c31b29fc6e9c5b970879",
    "SYN-RC-0002": "a440c9e483b5b536647ccdfbeca03c5706cfecf9c22f3b05dbe549c0ff5ac96a",
    "SYN-RC-0003": "2504ca7325d70af1911faa8fe81929654532f54db9c741a86d407f75d494bb13",
    "SYN-RC-0004": "fdb0ed9b4f1790fbd2bf353cdd3689c2bcd150335c226f26eaca53d12ad07b2c",
}


class SyntheticDemoSeedError(RuntimeError):
    """The fixed synthetic seed could not be applied safely."""


class SyntheticDemoPracticeBindingError(SyntheticDemoSeedError):
    """The configured PostgreSQL practice binding was not proven."""


class SyntheticDemoConflict(SyntheticDemoSeedError):
    """An approved case ID is already occupied by different content."""


@dataclass(frozen=True)
class PreparedSyntheticDemoCase:
    """One digest-pinned case from the approved pack."""

    case_id: str
    visit: dict[str, object]
    digest: str


@dataclass(frozen=True)
class PreparedSyntheticDemoSeed:
    """Validated, deterministic input for the one permitted seed operation."""

    cases: tuple[PreparedSyntheticDemoCase, ...]
    digest: str


def _canonical_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def prepare_synthetic_demo_seed() -> PreparedSyntheticDemoSeed:
    """Validate and digest the complete immutable bundled pack."""
    pack = load_demo_release_candidate()
    if pack.get("pack_id") != PACK_ID:
        raise SyntheticDemoSeedError("the bundled synthetic pack is not approved")

    pack_digest = _canonical_digest(pack)
    if pack_digest != EXPECTED_PACK_DIGEST:
        raise SyntheticDemoSeedError("the approved synthetic pack digest changed")

    cases = pack.get("cases")
    if not isinstance(cases, list):
        raise SyntheticDemoSeedError("the approved synthetic cases are unavailable")
    case_ids = tuple(case.get("case_id") for case in cases)
    if case_ids != CASE_IDS:
        raise SyntheticDemoSeedError("the approved synthetic cases changed")

    prepared_cases: list[PreparedSyntheticDemoCase] = []
    for case_id, case in zip(CASE_IDS, cases):
        visit = dict(case["visit"])
        if visit.get("encounter_id") != case_id:
            raise SyntheticDemoSeedError("an approved synthetic case is malformed")
        digest = _canonical_digest({
            "case_id": case_id,
            "pack_id": PACK_ID,
            "source": SOURCE,
            "visit": visit,
        })
        if digest != EXPECTED_CASE_DIGESTS[case_id]:
            raise SyntheticDemoSeedError(
                "an approved synthetic case digest changed"
            )
        prepared_cases.append(PreparedSyntheticDemoCase(
            case_id=case_id,
            visit=visit,
            digest=digest,
        ))

    return PreparedSyntheticDemoSeed(
        cases=tuple(prepared_cases),
        digest=pack_digest,
    )


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
    prepared: PreparedSyntheticDemoCase,
) -> dict[str, object]:
    visit = prepared.visit
    return {
        "external_id": prepared.case_id,
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


def _insert_case(connection, settings: Settings, prepared: PreparedSyntheticDemoCase):
    visit = prepared.visit
    return connection.execute(
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
            prepared.case_id,
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


def _load_persisted_case(connection, settings: Settings, case_id: str):
    return connection.execute(
        """SELECT external_id, patient_age, patient_sex, encounter_type,
                  place_of_service, intake_text, transcript, signed_note,
                  provider_specialty, submitted_em_code, submitted_icd10,
                  source, ingested_by_tenant_id, ingested_by_user_id, version
             FROM encounters
            WHERE practice_id = %s AND external_id = %s""",
        (settings.practice_id, case_id),
    ).fetchone()


def seed_synthetic_demo_release_candidate(settings: Settings) -> dict[str, object]:
    """Atomically insert the fixed pack, or no-op an exact replay."""
    # Artifact validation and all fixed digests are proven before constructing
    # a database context or opening a connection.
    prepared = prepare_synthetic_demo_seed()
    tenant_id = _validate_postgres_binding_settings(settings)
    context = PostgresContext(
        settings.database_url,
        practice_id=settings.practice_id,
    )
    inserted_count = 0
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
                  AND p.active""",
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

        for case in prepared.cases:
            inserted = _insert_case(connection, settings, case)
            if inserted is not None:
                inserted_count += 1
            stored = _load_persisted_case(
                connection,
                settings,
                case.case_id,
            )
            if not _row_is_exact_replay(
                stored,
                _expected_persisted_values(case),
            ):
                # Raising inside the connection context rolls back every prior
                # insert in this invocation, including earlier pack members.
                raise SyntheticDemoConflict(
                    "an approved synthetic case ID has conflicting content"
                )

    case_count = len(prepared.cases)
    return {
        "pack_id": PACK_ID,
        "case_count": case_count,
        "inserted_count": inserted_count,
        "replayed_count": case_count - inserted_count,
        "status": "inserted" if inserted_count else "replayed",
        "digest": prepared.digest,
    }


def _closed_result(
    *,
    status: str,
    digest: str,
    inserted_count: int = 0,
    replayed_count: int = 0,
) -> dict[str, object]:
    return {
        "pack_id": PACK_ID,
        "case_count": len(CASE_IDS),
        "inserted_count": inserted_count,
        "replayed_count": replayed_count,
        "status": status,
        "digest": digest,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixed seed without accepting or reflecting caller input."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    digest = EXPECTED_PACK_DIGEST
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
        # Never emit exception text, a DSN, or encounter content. Operators get
        # only fixed artifact identity, closed counts/digest, and status token.
        result = _closed_result(status="failed", digest=digest)
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
