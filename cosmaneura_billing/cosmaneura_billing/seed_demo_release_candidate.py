"""Seed the complete approved synthetic demo source set.

This command intentionally has no input-file or payload option. It loads and
validates the immutable base and source-extension packs before opening a
database connection, then inserts all twelve approved cases in one transaction.
An exact retry is a no-op; any occupied case ID with different persisted
content aborts the whole transaction without updating or partially inserting.

The command is intentionally restricted to the table-owning migration identity.
Migration 008 must install the database guards that prevent every non-owner
from claiming, mutating, deleting, or truncating the reserved synthetic-demo
provenance.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from .corpus.demo_release_candidate import load_demo_release_candidate
from .corpus.demo_source_extension import load_demo_source_extension
from .persistence import PostgresContext, SYNTHETIC_DEMO_SOURCE
from .settings import Settings


PACK_ID = "cosmaneura-demo-rc-v1"
EXTENSION_PACK_ID = "cosmaneura-demo-source-extension-v1"
SEED_ID = "cosmaneura-demo-rc-v1+source-extension-v1"
SOURCE = SYNTHETIC_DEMO_SOURCE
CASE_IDS = (
    "SYN-RC-0001",
    "SYN-RC-0002",
    "SYN-RC-0003",
    "SYN-RC-0004",
)
EXTENSION_CASE_IDS = (
    "SYN-RC-0005",
    "SYN-RC-0006",
    "SYN-RC-0007",
    "SYN-RC-0008",
    "SYN-RC-0009",
    "SYN-RC-0010",
    "SYN-RC-0011",
    "SYN-RC-0012",
)
SEED_CASE_IDS = CASE_IDS + EXTENSION_CASE_IDS
EXPECTED_PACK_DIGEST = (
    "556b4913553e7fddc8063f1c6d47b64e1cdec7a9e85ab873fdb81cc4e13be68f"
)
EXPECTED_CASE_DIGESTS = {
    "SYN-RC-0001": "c847409a34bc382634eea22a0de7c611d99effc73079c31b29fc6e9c5b970879",
    "SYN-RC-0002": "a440c9e483b5b536647ccdfbeca03c5706cfecf9c22f3b05dbe549c0ff5ac96a",
    "SYN-RC-0003": "2504ca7325d70af1911faa8fe81929654532f54db9c741a86d407f75d494bb13",
    "SYN-RC-0004": "fdb0ed9b4f1790fbd2bf353cdd3689c2bcd150335c226f26eaca53d12ad07b2c",
}
EXPECTED_EXTENSION_PACK_DIGEST = (
    "804a4b4c33d166604daacaab415a46871b97879b174beb33666e31f2b859e8bb"
)
EXPECTED_EXTENSION_CASE_DIGESTS = {
    "SYN-RC-0005": "e37fea284509a38e41a7ba8b91cbad1db61b282f3238065b3d6a60dc732d4e05",
    "SYN-RC-0006": "9408df5774f8c1f6d322c03549ab44dc923271003476845bce689c1f1de1449c",
    "SYN-RC-0007": "d2044bb683cf9500ce52eb3075424b412790768dda2b8b039c34689f345ada1e",
    "SYN-RC-0008": "4d73046b079b2e19e13c0bb2bede4fb879ba382d8e839dccd5b90857dede10db",
    "SYN-RC-0009": "ece109a602a57cabd6b5b5d09718a88702e082fc617aa22493104e8fffa0d3c8",
    "SYN-RC-0010": "116e3f36a4e5fbae152cda859a95c961893872ea5facccc5328640e2f8a2a830",
    "SYN-RC-0011": "d46bd1ffefa39108224ec2cad0197a932b27fa3390570f71953b4ca7616a7b52",
    "SYN-RC-0012": "e9f85fb8b20b0387b0cd1c16be95b9e08858c1faf03373f9ed7d10796e4d0095",
}
EXPECTED_SEED_DIGEST = (
    "3ef6297b7a1987477b491c27393772caea3e4c41eeb3fbca39b14de3e77cc304"
)
IMMUTABILITY_TRIGGER = "encounters_synthetic_demo_immutability"
IMMUTABILITY_TRUNCATE_TRIGGER = (
    "encounters_synthetic_demo_truncate_immutability"
)


class SyntheticDemoSeedError(RuntimeError):
    """The fixed synthetic seed could not be applied safely."""


class SyntheticDemoPracticeBindingError(SyntheticDemoSeedError):
    """The configured PostgreSQL practice binding was not proven."""


class SyntheticDemoDatabaseAuthorityError(SyntheticDemoSeedError):
    """The table-owner and database immutability guard were not proven."""


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
    """Validate and digest both immutable packs before any database access."""
    base_pack = load_demo_release_candidate()
    extension_pack = load_demo_source_extension()
    pack_specs = (
        (
            base_pack,
            PACK_ID,
            CASE_IDS,
            EXPECTED_PACK_DIGEST,
            EXPECTED_CASE_DIGESTS,
        ),
        (
            extension_pack,
            EXTENSION_PACK_ID,
            EXTENSION_CASE_IDS,
            EXPECTED_EXTENSION_PACK_DIGEST,
            EXPECTED_EXTENSION_CASE_DIGESTS,
        ),
    )

    prepared_cases: list[PreparedSyntheticDemoCase] = []
    verified_pack_digests: list[dict[str, str]] = []
    for (
        pack,
        pack_id,
        case_ids,
        expected_pack_digest,
        expected_case_digests,
    ) in pack_specs:
        if pack.get("pack_id") != pack_id:
            raise SyntheticDemoSeedError("a bundled synthetic pack is not approved")
        pack_digest = _canonical_digest(pack)
        if pack_digest != expected_pack_digest:
            raise SyntheticDemoSeedError("an approved synthetic pack digest changed")
        verified_pack_digests.append({"pack_id": pack_id, "digest": pack_digest})

        cases = pack.get("cases")
        if not isinstance(cases, list):
            raise SyntheticDemoSeedError(
                "the approved synthetic cases are unavailable"
            )
        if tuple(case.get("case_id") for case in cases) != case_ids:
            raise SyntheticDemoSeedError("the approved synthetic cases changed")

        for case_id, case in zip(case_ids, cases):
            visit = dict(case["visit"])
            if visit.get("encounter_id") != case_id:
                raise SyntheticDemoSeedError("an approved synthetic case is malformed")
            digest = _canonical_digest({
                "case_id": case_id,
                "pack_id": pack_id,
                "source": SOURCE,
                "visit": visit,
            })
            if digest != expected_case_digests[case_id]:
                raise SyntheticDemoSeedError(
                    "an approved synthetic case digest changed"
                )
            prepared_cases.append(PreparedSyntheticDemoCase(
                case_id=case_id,
                visit=visit,
                digest=digest,
            ))

    seed_digest = _canonical_digest({
        "seed_id": SEED_ID,
        "packs": verified_pack_digests,
        "cases": [
            {"case_id": case.case_id, "digest": case.digest}
            for case in prepared_cases
        ],
    })
    if seed_digest != EXPECTED_SEED_DIGEST:
        raise SyntheticDemoSeedError("the approved synthetic seed digest changed")
    if tuple(case.case_id for case in prepared_cases) != SEED_CASE_IDS:
        raise SyntheticDemoSeedError("the approved synthetic seed ordering changed")

    return PreparedSyntheticDemoSeed(
        cases=tuple(prepared_cases),
        digest=seed_digest,
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


def _validate_database_seed_authority(connection) -> None:
    """Require the exact table owner and exact migration-008 guard shape."""
    authority = connection.execute(
        """SELECT current_user::text AS current_user,
                  pg_catalog.pg_get_userbyid(relation.relowner)::text
                      AS table_owner,
                  EXISTS (
                      SELECT 1
                        FROM pg_catalog.pg_trigger AS row_guard
                        JOIN pg_catalog.pg_trigger AS truncate_guard
                          ON truncate_guard.tgrelid = row_guard.tgrelid
                        JOIN pg_catalog.pg_proc AS guard_function
                          ON guard_function.oid = row_guard.tgfoid
                       WHERE row_guard.tgrelid = relation.oid
                         AND row_guard.tgname = %s
                         AND NOT row_guard.tgisinternal
                         AND row_guard.tgenabled = 'A'
                         AND row_guard.tgtype = 31
                         AND row_guard.tgqual IS NULL
                         AND row_guard.tgattr = ''::pg_catalog.int2vector
                         AND row_guard.tgnargs = 0
                         AND truncate_guard.tgname = %s
                         AND NOT truncate_guard.tgisinternal
                         AND truncate_guard.tgenabled = 'A'
                         AND truncate_guard.tgtype = 34
                         AND truncate_guard.tgqual IS NULL
                         AND truncate_guard.tgattr = ''::pg_catalog.int2vector
                         AND truncate_guard.tgnargs = 0
                         AND truncate_guard.tgfoid = guard_function.oid
                         AND guard_function.oid = pg_catalog.to_regprocedure(
                             'public.cosmaneura_guard_synthetic_demo_encounter()'
                         )
                         AND guard_function.proowner = relation.relowner
                         AND NOT guard_function.prosecdef
                         AND guard_function.proconfig =
                             ARRAY['search_path=pg_catalog']::text[]
                         AND guard_function.pronargs = 0
                         AND guard_function.prorettype =
                             pg_catalog.to_regtype('pg_catalog.trigger')
                  ) AS immutability_guard
             FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = pg_catalog.to_regclass('public.encounters')""",
        (IMMUTABILITY_TRIGGER, IMMUTABILITY_TRUNCATE_TRIGGER),
    ).fetchone()
    if (
        authority is None
        or authority.get("current_user") != authority.get("table_owner")
        or authority.get("immutability_guard") is not True
    ):
        raise SyntheticDemoDatabaseAuthorityError(
            "the synthetic seed database authority was not proven"
        )


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
    """Atomically insert both fixed packs through the table-owning identity."""
    # Artifact validation and all fixed digests are proven before constructing
    # a database context or opening a connection.
    prepared = prepare_synthetic_demo_seed()
    return _seed_prepared_synthetic(settings, prepared, seed_id=SEED_ID)


def _seed_prepared_synthetic(
    settings: Settings, prepared: PreparedSyntheticDemoSeed, *, seed_id: str,
) -> dict[str, object]:
    """Persist an already verified fixed pack; no public payload admission."""
    tenant_id = _validate_postgres_binding_settings(settings)
    context = PostgresContext(
        settings.database_url,
        practice_id=settings.practice_id,
    )
    inserted_count = 0
    with context.connection() as connection:
        _validate_database_seed_authority(connection)
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
        "pack_id": seed_id,
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
        "pack_id": SEED_ID,
        "case_count": len(SEED_CASE_IDS),
        "inserted_count": inserted_count,
        "replayed_count": replayed_count,
        "status": status,
        "digest": digest,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixed seed without accepting or reflecting caller input."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    digest = EXPECTED_SEED_DIGEST
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
    except (
        SyntheticDemoDatabaseAuthorityError,
        SyntheticDemoPracticeBindingError,
    ):
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
