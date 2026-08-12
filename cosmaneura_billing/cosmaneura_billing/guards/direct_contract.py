"""F3 — the direct-contract hard gate.

Grace (MCD billing, Aug 6): "If we could somehow ensure that we know if it's a
direct-contract patient so that we never… charge them a self-pay fee."

Every pathway that could charge or dun a patient calls
``check_direct_contract`` before acting. Semantics (CLAUDE.md constraint #6):

  * ``is_direct_contract == True``  → hard stop. The caller may not proceed to
    any self-pay charge or outreach.
  * Unknown / missing / unreadable list → **fail closed**: the check reports
    ``is_direct_contract=True`` with ``status="unknown_fail_closed"`` so the
    caller blocks AND the item surfaces for human review. A gate that cannot
    verify must behave as if the patient matched.

The list *source* is deliberately an adapter: today a versioned local JSON
file (synthetic; the practice's real list location is an Aug 13 question).
Swapping the source never changes the gate semantics.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_BUNDLED_LIST = Path(__file__).resolve().parent.parent / "data" / "direct_contract_list.v1.json"

STATUS_MATCH = "match"
STATUS_NO_MATCH = "no_match"
STATUS_UNKNOWN = "unknown_fail_closed"


def _norm(ref: str) -> str:
    return (ref or "").strip().upper()


@dataclass(frozen=True)
class DirectContractCheck:
    """Result of the gate. ``is_direct_contract`` means "must be treated as a
    direct-contract patient for charging purposes" — on an unverifiable list it
    is True even though the underlying fact is unknown (fail closed)."""
    is_direct_contract: bool
    source: str
    checked_at: str
    status: str  # match | no_match | unknown_fail_closed

    @property
    def blocked(self) -> bool:
        return self.is_direct_contract

    @property
    def needs_human_review(self) -> bool:
        return self.status == STATUS_UNKNOWN

    def to_json(self) -> dict:
        return {
            "is_direct_contract": self.is_direct_contract,
            "source": self.source,
            "checked_at": self.checked_at,
            "status": self.status,
        }


class PracticeListSource:
    """Versioned local list file: {"version": "practice_list.v1",
    "patient_refs": ["…", …]}. Missing or malformed ⇒ ``available() is False``
    and the gate fails closed."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _BUNDLED_LIST
        self._refs: Optional[frozenset[str]] = None
        self._version = "unavailable"
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            refs = raw["patient_refs"]
            if not isinstance(refs, list):
                raise TypeError("patient_refs must be a list")
            self._refs = frozenset(_norm(r) for r in refs if _norm(r))
            self._version = str(raw.get("version", "practice_list.v?"))
        except Exception:
            self._refs = None  # unreadable ⇒ gate fails closed

    def available(self) -> bool:
        return self._refs is not None

    @property
    def version(self) -> str:
        return self._version if self.available() else "unavailable"

    def contains(self, patient_ref: str) -> bool:
        if self._refs is None:
            raise RuntimeError("list unavailable — caller must fail closed")
        return _norm(patient_ref) in self._refs


def check_direct_contract(
    patient_ref: str,
    source: PracticeListSource | None = None,
    *,
    now: datetime | None = None,
) -> DirectContractCheck:
    """The gate. Call before ANY self-pay charge or dunning outreach."""
    checked_at = (now or datetime.now(timezone.utc)).isoformat()
    src = source if source is not None else PracticeListSource()

    if not _norm(patient_ref) or not src.available():
        # Cannot verify ⇒ behave as a match: block and surface for review.
        return DirectContractCheck(
            is_direct_contract=True, source=src.version,
            checked_at=checked_at, status=STATUS_UNKNOWN,
        )

    matched = src.contains(patient_ref)
    return DirectContractCheck(
        is_direct_contract=matched, source=src.version, checked_at=checked_at,
        status=STATUS_MATCH if matched else STATUS_NO_MATCH,
    )
