"""Digest-pinned synthetic insurance follow-up cases for the pilot demo.

The bundled contact destinations use the NANP fictional 555-0100--0199 range.
They are returned only when the persisted chase row exactly matches the fixed
case that produced it. Operational rows receive a system-of-record placeholder;
this module is not a general patient-contact directory.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..direct_contract import DirectContractList
from .detector import detect
from .models import ChaseItem, CoverageSnapshot, State, Trigger


PACK_ID = "cosmaneura-synthetic-chase-v1"
SEED_ID = "cosmaneura-synthetic-chase-seed-v1"
EXPECTED_PACK_DIGEST = (
    "fc207ef1419882f48dce5b12dae05233de2f99d9ea930ee5eb84d84e659d23ff"
)
EXPECTED_CASE_IDS = (
    "SYN-CHASE-0001",
    "SYN-CHASE-0002",
    "SYN-CHASE-0003",
    "SYN-CHASE-0004",
)
EXPECTED_TRIGGERS = (
    Trigger.NO_CARD_ON_FILE,
    Trigger.EXPIRED,
    Trigger.ELIGIBILITY_INACTIVE,
    Trigger.NO_CARD_ON_FILE,
)
EXPECTED_STATES = (
    State.DETECTED,
    State.DETECTED,
    State.DETECTED,
    State.NEEDS_REVIEW,
)
_PACK_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "synthetic_chase"
    / "pack.v1.json"
)
_FIXED_PHONE = re.compile(r"\+1 202-555-01[0-9]{2}\Z")
_PROVENANCE_KEY = "synthetic_demo_provenance"


class SyntheticChaseSeedError(RuntimeError):
    """The fixed synthetic chase pack is missing, malformed, or changed."""


@dataclass(frozen=True)
class SyntheticContactTask:
    channel: str
    destination: str
    instruction: str


@dataclass(frozen=True)
class PreparedSyntheticChaseCase:
    case_id: str
    digest: str
    display_label: str
    item: ChaseItem
    contact: SyntheticContactTask

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "schema": "cosmaneura.synthetic-chase-provenance.v1",
            "seed_id": SEED_ID,
            "pack_digest": EXPECTED_PACK_DIGEST,
            "case_id": self.case_id,
            "case_digest": self.digest,
        }


@dataclass(frozen=True)
class PreparedSyntheticChaseSeed:
    seed_id: str
    digest: str
    as_of_date: str
    detected_at: str
    cases: tuple[PreparedSyntheticChaseCase, ...]

    @property
    def items(self) -> tuple[ChaseItem, ...]:
        # Domain rows are mutable after seeding. Return fresh copies so one
        # demo action cannot alter the digest-validated templates or a retry.
        return tuple(ChaseItem.from_dict(case.item.to_dict()) for case in self.cases)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_pack() -> dict[str, Any]:
    try:
        value = json.loads(_PACK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyntheticChaseSeedError(
            "the approved synthetic chase pack is unavailable"
        ) from exc
    if not isinstance(value, dict):
        raise SyntheticChaseSeedError("the approved synthetic chase pack is malformed")
    return value


def _strict_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise SyntheticChaseSeedError(f"the approved synthetic {label} changed")


@lru_cache(maxsize=1)
def prepare_synthetic_chase_seed() -> PreparedSyntheticChaseSeed:
    """Validate the immutable pack and deterministically build all four rows."""
    pack = _load_pack()
    _strict_keys(
        pack,
        {
            "pack_id", "schema_version", "data_class", "as_of_date",
            "detected_at", "cases",
        },
        label="chase pack",
    )
    if (
        pack.get("pack_id") != PACK_ID
        or pack.get("schema_version") != 1
        or pack.get("data_class") != "synthetic_demo"
    ):
        raise SyntheticChaseSeedError("the bundled synthetic chase pack is not approved")
    digest = _canonical_digest(pack)
    if digest != EXPECTED_PACK_DIGEST:
        raise SyntheticChaseSeedError("the approved synthetic chase pack digest changed")

    as_of_date = pack.get("as_of_date")
    detected_at = pack.get("detected_at")
    raw_cases = pack.get("cases")
    if (
        not isinstance(as_of_date, str)
        or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", as_of_date)
        or not isinstance(detected_at, str)
        or not detected_at.endswith("+00:00")
        or not isinstance(raw_cases, list)
        or len(raw_cases) != 4
    ):
        raise SyntheticChaseSeedError("the approved synthetic chase pack is malformed")

    direct_refs = {
        str(case.get("patient_ref"))
        for case in raw_cases
        if isinstance(case, dict) and case.get("direct_contract") is True
    }
    if len(direct_refs) != 1:
        raise SyntheticChaseSeedError("the synthetic human-review case changed")
    roster = DirectContractList(refs=direct_refs, version=PACK_ID)
    clock = lambda: detected_at  # noqa: E731 - deterministic injected clock
    prepared: list[PreparedSyntheticChaseCase] = []
    seen_refs: set[str] = set()

    case_keys = {
        "case_id", "patient_ref", "display_label", "expected_trigger",
        "expected_state", "direct_contract", "snapshot", "contact",
    }
    snapshot_fields = set(CoverageSnapshot.__dataclass_fields__) - {"patient_ref"}
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise SyntheticChaseSeedError("a synthetic chase case is malformed")
        _strict_keys(raw_case, case_keys, label="chase case")
        case_id = raw_case.get("case_id")
        patient_ref = raw_case.get("patient_ref")
        label = raw_case.get("display_label")
        if (
            case_id != EXPECTED_CASE_IDS[index]
            or not isinstance(patient_ref, str)
            or not patient_ref.startswith("synthetic-chase-patient-")
            or patient_ref in seen_refs
            or not isinstance(label, str)
            or not label.startswith("Synthetic patient ")
        ):
            raise SyntheticChaseSeedError("the approved synthetic chase cases changed")
        seen_refs.add(patient_ref)

        snapshot_payload = raw_case.get("snapshot")
        if (
            not isinstance(snapshot_payload, dict)
            or not set(snapshot_payload).issubset(snapshot_fields)
            or any(key.endswith("consent") for key in snapshot_payload)
            or any(key in snapshot_payload for key in ("email", "phone"))
        ):
            raise SyntheticChaseSeedError("a synthetic coverage snapshot is malformed")
        try:
            snapshot = CoverageSnapshot(
                patient_ref=patient_ref,
                **snapshot_payload,
            )
        except TypeError as exc:
            raise SyntheticChaseSeedError(
                "a synthetic coverage snapshot is malformed"
            ) from exc

        item = detect(
            snapshot,
            today=as_of_date,
            now=clock,
            roster=roster,
        )
        expected_trigger = EXPECTED_TRIGGERS[index]
        expected_state = EXPECTED_STATES[index]
        if (
            item is None
            or item.trigger is not expected_trigger
            or item.state is not expected_state
            or raw_case.get("expected_trigger") != expected_trigger.value
            or raw_case.get("expected_state") != expected_state.value
            or bool(raw_case.get("direct_contract"))
                != bool(item.direct_contract.get("is_direct_contract"))
        ):
            raise SyntheticChaseSeedError(
                "a synthetic chase case no longer produces its approved result"
            )
        case_digest = _canonical_digest(raw_case)
        provenance = {
            "schema": "cosmaneura.synthetic-chase-provenance.v1",
            "seed_id": SEED_ID,
            "pack_digest": EXPECTED_PACK_DIGEST,
            "case_id": case_id,
            "case_digest": case_digest,
        }
        # This field is server-owned: posted detection accepts coverage facts,
        # never direct-contract/gate payloads. Persisting the exact digest here
        # prevents an operational lookalike from unlocking a demo destination.
        item.direct_contract = {
            **item.direct_contract,
            _PROVENANCE_KEY: provenance,
        }

        contact_payload = raw_case.get("contact")
        if not isinstance(contact_payload, dict):
            raise SyntheticChaseSeedError("a synthetic contact task is malformed")
        _strict_keys(
            contact_payload,
            {"channel", "destination", "instruction"},
            label="contact task",
        )
        channel = contact_payload.get("channel")
        destination = contact_payload.get("destination")
        instruction = contact_payload.get("instruction")
        if (
            channel != "phone"
            or not isinstance(destination, str)
            or _FIXED_PHONE.fullmatch(destination) is None
            or not isinstance(instruction, str)
            or not 1 <= len(instruction) <= 240
        ):
            raise SyntheticChaseSeedError("a synthetic contact task is malformed")
        prepared.append(PreparedSyntheticChaseCase(
            case_id=case_id,
            digest=case_digest,
            display_label=label,
            item=item,
            contact=SyntheticContactTask(
                channel=channel,
                destination=destination,
                instruction=instruction,
            ),
        ))

    return PreparedSyntheticChaseSeed(
        seed_id=SEED_ID,
        digest=digest,
        as_of_date=as_of_date,
        detected_at=detected_at,
        cases=tuple(prepared),
    )


def synthetic_case_for(item: ChaseItem) -> PreparedSyntheticChaseCase | None:
    """Return a case only for exact server-owned, digest-bound provenance."""
    seed = prepare_synthetic_chase_seed()
    return next((
        case for case in seed.cases
        if case.item.chase_id == item.chase_id
        and case.item.patient_ref == item.patient_ref
        and case.item.trigger is item.trigger
        and item.direct_contract.get(_PROVENANCE_KEY) == case.provenance
    ), None)


__all__ = [
    "EXPECTED_PACK_DIGEST",
    "PACK_ID",
    "SEED_ID",
    "PreparedSyntheticChaseCase",
    "PreparedSyntheticChaseSeed",
    "SyntheticChaseSeedError",
    "prepare_synthetic_chase_seed",
    "synthetic_case_for",
]
