"""Load only the fixed MCD synthetic staff-training sources into PostgreSQL.

The command accepts no arguments or caller payload. The packaged source and
seed digests are verified before database access. Existing owner, practice and
reserved-source guards apply; one conflict rolls back the entire twelve-case
insertion and an exact retry leaves every row unchanged.
"""
from __future__ import annotations

import json
import sys
from typing import Sequence

from . import seed_demo_release_candidate as fixed_seed
from .corpus.mcd_training import CASE_IDS, PACK_ID, load_mcd_training_pack
from .settings import Settings


EXPECTED_SEED_DIGEST = (
    "e62f6df781b8cba1b43eb8847e4e7e1f7b8f92a212ad49d2a48c953609c36594"
)


def prepare_mcd_training_seed() -> fixed_seed.PreparedSyntheticDemoSeed:
    """Bind the exact source cases to the existing protected synthetic lane."""
    pack = load_mcd_training_pack()
    if set(pack) != {"pack_id", "cases"} or pack["pack_id"] != PACK_ID:
        raise fixed_seed.SyntheticDemoSeedError("the MCD training pack changed")
    raw_cases = pack["cases"]
    if (
        not isinstance(raw_cases, list)
        or any(not isinstance(case, dict) for case in raw_cases)
        or tuple(case.get("case_id") for case in raw_cases) != CASE_IDS
    ):
        raise fixed_seed.SyntheticDemoSeedError("the MCD training cases changed")
    cases = []
    for case in raw_cases:
        if set(case) != {"case_id", "visit"} or not isinstance(case["visit"], dict):
            raise fixed_seed.SyntheticDemoSeedError("an MCD training case changed")
        visit = dict(case["visit"])
        if visit.get("encounter_id") != case["case_id"]:
            raise fixed_seed.SyntheticDemoSeedError("an MCD training identity changed")
        digest = fixed_seed._canonical_digest({
            "case_id": case["case_id"], "pack_id": PACK_ID,
            "source": fixed_seed.SOURCE, "visit": visit,
        })
        cases.append(fixed_seed.PreparedSyntheticDemoCase(case["case_id"], visit, digest))
    digest = fixed_seed._canonical_digest({
        "seed_id": PACK_ID,
        "cases": [{"case_id": case.case_id, "digest": case.digest} for case in cases],
    })
    if digest != EXPECTED_SEED_DIGEST:
        raise fixed_seed.SyntheticDemoSeedError("the MCD training seed digest changed")
    return fixed_seed.PreparedSyntheticDemoSeed(tuple(cases), digest)


def seed_mcd_training(settings: Settings) -> dict[str, object]:
    """Insert all fixed cases atomically as the exact encounters table owner."""
    prepared = prepare_mcd_training_seed()
    return fixed_seed._seed_prepared_synthetic(settings, prepared, seed_id=PACK_ID)


def _closed_result(status: str) -> dict[str, object]:
    return {
        "pack_id": PACK_ID, "case_count": len(CASE_IDS),
        "inserted_count": 0, "replayed_count": 0,
        "status": status, "digest": EXPECTED_SEED_DIGEST,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(json.dumps(_closed_result("arguments_rejected"), sort_keys=True))
        return 2
    try:
        result = seed_mcd_training(Settings.from_env())
    except fixed_seed.SyntheticDemoConflict:
        result, exit_code = _closed_result("conflict"), 1
    except (fixed_seed.SyntheticDemoDatabaseAuthorityError,
            fixed_seed.SyntheticDemoPracticeBindingError):
        result, exit_code = _closed_result("binding_rejected"), 1
    except Exception:
        # Never print a DSN, source text, exception detail or caller argument.
        result, exit_code = _closed_result("failed"), 1
    else:
        exit_code = 0
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
