"""Direct-contract gate (F3) — the hard stop before money or outreach.

Grace Targonski, MCD Director of Billing, 2026-08-06: *"If we could somehow
ensure that we know if it's a direct-contract patient so that we never… charge
them a self-pay fee."* Billing a direct-contract patient a self-pay fee is not a
billing error to the patient — it is a broken promise, and it strains the
contract the practice signed.

Two callers, two policies
-------------------------
The gate reports a fact; each pathway applies its own policy to that fact,
because the correct response differs:

* **Charging or dunning** (F5 self-pay chase) — a match is a **hard stop**. The
  caller may not proceed, at all.
* **Insurance-data chase** (F9) — a match is routed to **human review**, not
  auto-sequenced. The practice may not bill insurance for these patients, so
  chasing their card is probably pointless; but "probably" is not a basis for
  silently dropping someone off a worklist. Whether they should be chased at all
  is an open question from the Aug 12 synthesis (§8 Q11) that the Aug 13
  follow-up did not answer.

Fail closed
-----------
An unknown answer is treated as blocked. If the list is missing, unreadable, or
does not mention the patient, this returns ``known=False`` and every policy
treats that as "do not proceed unattended". The failure mode of guessing wrong
here is charging a patient the practice promised not to charge; the failure mode
of failing closed is a staff member looking at one row. Those are not
comparable, so the code does not treat them as comparable.

The list source of truth is still open (§8 Q5). This reads a versioned local
file; when the real source appears, only ``DirectContractList`` changes — the
gate semantics do not.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

# Where the practice drops its list. CSV or JSON; one patient reference per row.
DEFAULT_LIST_PATH = "direct_contract_patients.csv"
LIST_VERSION_UNKNOWN = "unavailable"


@dataclass(frozen=True)
class DirectContractCheck:
    """The gate's answer. ``known`` is as important as ``is_direct_contract``."""
    patient_ref: str
    is_direct_contract: bool
    known: bool
    source: str
    checked_at: str

    @property
    def blocked(self) -> bool:
        """True when no automated money or outreach action may proceed.

        Note this is ``True`` for an unknown answer as well as a match — that is
        the fail-closed rule, expressed once so no caller can forget it.
        """
        return self.is_direct_contract or not self.known

    def to_dict(self) -> dict:
        return {
            "patient_ref": self.patient_ref,
            "is_direct_contract": self.is_direct_contract,
            "known": self.known,
            "blocked": self.blocked,
            "source": self.source,
            "checked_at": self.checked_at,
        }


class DirectContractList:
    """The practice's direct-contract roster, loaded from a versioned local file.

    Accepts a CSV with a ``patient_ref`` column (extra columns ignored) or a JSON
    list of refs / objects. A file that cannot be read is not an error the caller
    has to handle — it makes every answer ``known=False``, which blocks.
    """

    def __init__(self, path: str | os.PathLike | None = None,
                 refs: Optional[Iterable[str]] = None,
                 version: str = ""):
        self._explicit = {self._normalize(r) for r in refs} if refs is not None else None
        self._path = Path(path or os.environ.get(
            "COSMANEURA_DIRECT_CONTRACT_LIST", DEFAULT_LIST_PATH))
        self._version = version
        self._loaded: Optional[set[str]] = None
        self._load_error: str = ""

    @staticmethod
    def _normalize(ref: str) -> str:
        return (ref or "").strip().casefold()

    @property
    def available(self) -> bool:
        self._ensure()
        return self._loaded is not None

    @property
    def source(self) -> str:
        if self._explicit is not None:
            return f"in_memory:{self._version or 'test'}"
        self._ensure()
        if self._loaded is None:
            return f"{LIST_VERSION_UNKNOWN}:{self._load_error or 'not found'}"
        return f"{self._path.name}:{self._version or 'v1'}"

    def _ensure(self) -> None:
        if self._explicit is not None:
            self._loaded = self._explicit
            return
        if self._loaded is not None or self._load_error:
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as e:
            self._load_error = type(e).__name__
            return
        try:
            self._loaded = self._parse(text)
        except (ValueError, KeyError) as e:
            self._load_error = f"unparseable ({type(e).__name__})"

    @classmethod
    def _parse(cls, text: str) -> set[str]:
        stripped = text.lstrip()
        if stripped.startswith(("[", "{")):
            data = json.loads(stripped)
            rows = data if isinstance(data, list) else data.get("patients", [])
            return {cls._normalize(r if isinstance(r, str) else r.get("patient_ref", ""))
                    for r in rows} - {""}
        reader = csv.DictReader(text.splitlines())
        if not reader.fieldnames or "patient_ref" not in reader.fieldnames:
            raise ValueError("CSV needs a patient_ref column")
        return {cls._normalize(row.get("patient_ref", "")) for row in reader} - {""}

    def contains(self, patient_ref: str) -> Optional[bool]:
        """True / False, or None when the list is unavailable."""
        self._ensure()
        if self._loaded is None:
            return None
        return self._normalize(patient_ref) in self._loaded


def check_direct_contract(
    patient_ref: str,
    *,
    roster: Optional[DirectContractList] = None,
    now: Optional[Callable[[], str]] = None,
) -> DirectContractCheck:
    """Answer the gate question for one patient.

    ``now`` is injected so callers (and tests) control the timestamp rather than
    the module reading a clock.
    """
    roster = roster if roster is not None else DirectContractList()
    checked_at = (now or _default_now)()
    present = roster.contains(patient_ref)
    if present is None:
        return DirectContractCheck(
            patient_ref=patient_ref, is_direct_contract=False, known=False,
            source=roster.source, checked_at=checked_at)
    return DirectContractCheck(
        patient_ref=patient_ref, is_direct_contract=present, known=True,
        source=roster.source, checked_at=checked_at)


def _default_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Policies — the two ways callers consume the fact
# --------------------------------------------------------------------------- #
class DirectContractBlocked(PermissionError):
    """Raised when a charging or dunning pathway hits the gate.

    An exception rather than a return value on purpose: a caller that forgets to
    check a boolean charges the patient. A caller that forgets to catch this
    crashes, which is the safe direction.
    """

    def __init__(self, check: DirectContractCheck, action: str):
        self.check = check
        reason = ("patient is on the direct-contract list" if check.is_direct_contract
                  else f"direct-contract status unknown (source: {check.source})")
        super().__init__(
            f"refusing to {action} for {check.patient_ref}: {reason}. "
            "The practice has contracted not to bill these patients directly; an "
            "unknown answer fails closed for the same reason.")


def guard_patient_charge(
    patient_ref: str, *, action: str = "charge a self-pay fee", **kw
) -> DirectContractCheck:
    """Hard gate for any pathway that could charge or dun a patient (F5).

    Raises ``DirectContractBlocked`` on a match **or** an unknown answer.
    """
    check = check_direct_contract(patient_ref, **kw)
    if check.blocked:
        raise DirectContractBlocked(check, action)
    return check


def chase_routing(patient_ref: str, **kw) -> tuple[DirectContractCheck, str]:
    """Routing decision for the insurance-data chase (F9a).

    Returns the check and one of:
      * ``"auto"``   — not direct-contract, list available: may enter the sequence.
      * ``"review"`` — direct-contract, or status unknown: a human decides.

    Never ``"suppress"``. Silently dropping a patient off the worklist would hide
    the open policy question rather than surface it.
    """
    check = check_direct_contract(patient_ref, **kw)
    return check, ("review" if check.blocked else "auto")
