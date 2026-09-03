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
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable, Optional

from .direct_contract_sources import DirectContractSnapshot, DirectContractSource
from .settings import Settings

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
    source_fingerprint: str
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
            "gate_fingerprint": self.gate_fingerprint,
            "checked_at": self.checked_at,
        }

    @property
    def gate_fingerprint(self) -> str:
        """Bind a human release to the exact roster answer, not its timestamp."""
        value = json.dumps({
            "schema": "cosmaneura.direct-contract-gate.v1",
            "known": self.known,
            "is_direct_contract": self.is_direct_contract,
            "source_fingerprint": self.source_fingerprint,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DirectContractList:
    """The practice's direct-contract roster, loaded from a versioned local file.

    Accepts a CSV with a ``patient_ref`` column (extra columns ignored) or a JSON
    list of refs / objects. A file that cannot be read is not an error the caller
    has to handle — it makes every answer ``known=False``, which blocks.
    """

    def __init__(self, path: str | os.PathLike | None = None,
                 refs: Optional[Iterable[str]] = None,
                 version: str = "", settings: Settings | None = None,
                 fingerprint_key: str = "",
                 source: DirectContractSource | None = None):
        # A pluggable source (F3, synthesis §8 Q5) is mutually exclusive with an
        # explicit in-memory roster; both would define the same content twice.
        if source is not None and refs is not None:
            raise ValueError(
                "direct-contract roster cannot take both refs and a source")
        self._source = source
        self._explicit = {self._normalize(r) for r in refs} if refs is not None else None
        if source is not None:
            # The path is unused when a source is supplied, but kept assigned so
            # every accessor has a stable attribute to read.
            resolved_path = DEFAULT_LIST_PATH
        elif path is not None:
            resolved_path = path
        elif settings is not None:
            resolved_path = settings.direct_contract_list
        elif refs is not None:
            # Explicit test/demo rosters never consult process configuration.
            resolved_path = DEFAULT_LIST_PATH
        else:
            resolved_path = Settings.from_env().direct_contract_list
        self._path = Path(resolved_path)
        self._version = version
        self._fingerprint_key = (
            settings.chase_gate_hmac_key if settings is not None
            else fingerprint_key
        ).encode("utf-8")
        self._loaded: Optional[set[str]] = None
        self._load_error: str = ""
        self._source_fingerprint: str = ""
        self._source_label: str = ""
        self._loaded_signature: tuple[int, int, int, int] | None = None
        self._lock = RLock()

    @staticmethod
    def _normalize(ref: str) -> str:
        return (ref or "").strip().casefold()

    @property
    def available(self) -> bool:
        with self._lock:
            self._ensure()
            return self._loaded is not None

    @property
    def source(self) -> str:
        with self._lock:
            self._ensure()
            return self._source_value()

    def _source_value(self) -> str:
        if self._source is not None:
            if self._loaded is None:
                return f"{LIST_VERSION_UNKNOWN}:{self._load_error or 'unavailable'}"
            return self._source_label or "source:v1"
        if self._explicit is not None:
            return f"in_memory:{self._version or 'test'}"
        if self._loaded is None:
            return f"{LIST_VERSION_UNKNOWN}:{self._load_error or 'not found'}"
        return f"{self._path.name}:{self._version or 'v1'}"

    @property
    def source_fingerprint(self) -> str:
        """Opaque identity of the normalized roster content and source version."""
        with self._lock:
            self._ensure()
            return self._fingerprint_value()

    def _fingerprint_value(self) -> str:
        if not self._source_fingerprint:
            marker = json.dumps({
                "schema": "cosmaneura.direct-contract-source.v1",
                "source": self._source_value(),
                "available": self._loaded is not None,
            }, sort_keys=True, separators=(",", ":"))
            self._source_fingerprint = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        return self._source_fingerprint

    def _snapshot_label(self, snapshot: object) -> str:
        name = getattr(snapshot, "source_name", "") or "source"
        version = getattr(snapshot, "version", "") or "v1"
        return f"{name}:{version}"

    def _ensure_from_source(self) -> None:
        """Load through an injected source, fail-closed on anything uncertain.

        Reuses the same ``_fingerprint_source`` marker scheme as the file path,
        so a keyed roster fingerprint still binds a human release to the exact
        roster answer for external sources.
        """
        try:
            snapshot = self._source.load()  # type: ignore[union-attr]
        except Exception:
            self._loaded = None
            self._source_fingerprint = ""
            self._load_error = "source_error"
            return
        if not isinstance(snapshot, DirectContractSnapshot) or snapshot.refs is None:
            self._loaded = None
            self._source_fingerprint = ""
            self._load_error = (
                getattr(snapshot, "load_error", "") or "unavailable"
                if snapshot is not None else "source_error"
            )
            if snapshot is not None:
                self._source_label = self._snapshot_label(snapshot)
            return
        # Always rebuild from the freshly loaded snapshot. We deliberately do
        # NOT skip on a source-supplied content signature: an external source
        # could return a stale/empty signature with changed refs, which would
        # freeze the roster and fail OPEN for a newly-added direct-contract
        # patient. A source that wants to avoid repeated work caches inside its
        # own load() (see CallableDirectContractSource).
        self._loaded = {self._normalize(r) for r in snapshot.refs} - {""}
        self._load_error = ""
        self._source_label = self._snapshot_label(snapshot)
        self._source_fingerprint = self._fingerprint_source({
            "kind": "source",
            "name": snapshot.source_name or "source",
            "version": snapshot.version or "v1",
            # Mirrors how the file path folds size+mtime into its marker: the
            # content signature makes the roster fingerprint content-sensitive
            # even without a key, so a human release cannot carry across a
            # roster content change.
            "content_signature": snapshot.content_signature,
        }, refs=self._loaded)

    def _ensure(self) -> None:
        if self._source is not None:
            self._ensure_from_source()
            return
        if self._explicit is not None:
            self._loaded = self._explicit
            self._source_fingerprint = self._fingerprint_source({
                "kind": "explicit",
                "version": self._version or "test",
            }, refs=self._loaded)
            return

        try:
            path_stat = self._path.stat()
        except OSError as exc:
            self._loaded = None
            self._loaded_signature = None
            self._source_fingerprint = ""
            self._load_error = type(exc).__name__
            return
        path_signature = self._stat_signature(path_stat)
        if not self._fingerprint_key and self._loaded_signature == path_signature:
            return

        try:
            with self._path.open("r", encoding="utf-8") as source:
                opened_stat = os.fstat(source.fileno())
                text = source.read()
        except (OSError, UnicodeError) as exc:
            self._loaded = None
            self._loaded_signature = None
            self._source_fingerprint = ""
            self._load_error = type(exc).__name__
            return
        opened_signature = self._stat_signature(opened_stat)
        try:
            self._loaded = self._parse(text)
            self._load_error = ""
            self._loaded_signature = opened_signature
            self._source_fingerprint = self._fingerprint_source({
                "kind": "file",
                "name": self._path.name,
                "version": self._version or "v1",
                "size": opened_stat.st_size,
                "mtime_ns": opened_stat.st_mtime_ns,
            }, refs=self._loaded)
        except (ValueError, KeyError) as exc:
            self._loaded = None
            self._loaded_signature = opened_signature
            self._source_fingerprint = ""
            self._load_error = f"unparseable ({type(exc).__name__})"

    @staticmethod
    def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int]:
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)

    def _fingerprint_source(
        self, marker: dict[str, object], *, refs: set[str] | None = None,
    ) -> str:
        document = {
            "schema": "cosmaneura.direct-contract-source.v1",
            **marker,
        }
        if self._fingerprint_key:
            # Patient references are covered only by a secret-keyed digest;
            # they never become a reversible low-entropy hash in persistence.
            document["refs"] = sorted(refs or set())
        value = json.dumps(
            document, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if self._fingerprint_key:
            return hmac.new(self._fingerprint_key, value, hashlib.sha256).hexdigest()
        return hashlib.sha256(value).hexdigest()

    @classmethod
    def _parse(cls, text: str) -> set[str]:
        stripped = text.lstrip()
        if stripped.startswith(("[", "{")):
            data = json.loads(stripped)
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict) and isinstance(data.get("patients"), list):
                rows = data["patients"]
            else:
                raise ValueError("JSON roster must be a list of patient references")
            refs: set[str] = set()
            for row in rows:
                if isinstance(row, str):
                    ref = row
                elif isinstance(row, dict) and isinstance(row.get("patient_ref"), str):
                    ref = row["patient_ref"]
                else:
                    raise ValueError("JSON roster contains an invalid patient row")
                normalized = cls._normalize(ref)
                if normalized:
                    refs.add(normalized)
            return refs
        reader = csv.DictReader(text.splitlines())
        if not reader.fieldnames or "patient_ref" not in reader.fieldnames:
            raise ValueError("CSV needs a patient_ref column")
        return {cls._normalize(row.get("patient_ref", "")) for row in reader} - {""}

    def contains(self, patient_ref: str) -> Optional[bool]:
        """True / False, or None when the list is unavailable."""
        present, _, _ = self.lookup(patient_ref)
        return present

    def lookup(self, patient_ref: str) -> tuple[Optional[bool], str, str]:
        """Return answer, provenance, and fingerprint from one locked generation."""
        with self._lock:
            self._ensure()
            present = (
                None if self._loaded is None
                else self._normalize(patient_ref) in self._loaded
            )
            return present, self._source_value(), self._fingerprint_value()


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
    present, source, source_fingerprint = roster.lookup(patient_ref)
    if present is None:
        return DirectContractCheck(
            patient_ref=patient_ref, is_direct_contract=False, known=False,
            source=source, source_fingerprint=source_fingerprint,
            checked_at=checked_at)
    return DirectContractCheck(
        patient_ref=patient_ref, is_direct_contract=present, known=True,
        source=source, source_fingerprint=source_fingerprint,
        checked_at=checked_at)


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
