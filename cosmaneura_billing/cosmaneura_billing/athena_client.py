"""athenaNet client — mock-first. ALL Athena protocol knowledge lives here.

CLAUDE.md constraint #7: this module is the single place that knows how to
talk to athenaNet. Everything else in the codebase depends on the interface,
never the protocol. Today the only implementation is ``MockAthenaNet`` — a
deterministic synthetic harness; the real client arrives with sandbox →
Marketplace access and slots in behind the same methods.

Design notes
------------
* **Zero PHI.** Patients are fictional ("Synthetic Patient 007"), member IDs
  exist only in masked form (``…4821``) — the mock cannot leak what it never
  holds.
* **Deterministic.** Seeded RNG + injected ``now``; the same seed reproduces
  the same world, so detector tests assert exact trigger sets.
* **Eligibility mix by design:** ~10% of insured patients return INACTIVE on a
  271 check and ~5% time out — the F9a detector's demo fuel.
* **Idempotent writes.** ``write_insurance`` / ``attach_document`` accept an
  idempotency key and report duplicates instead of double-writing (mirrors the
  CardCapture pipeline's idempotent-write requirement).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

ELIGIBILITY_ACTIVE = "ACTIVE"
ELIGIBILITY_INACTIVE = "INACTIVE"
ELIGIBILITY_TIMEOUT = "TIMEOUT"

DOC_TYPE_INSURANCE_CARD = "ADMIN_INSURANCECARD"
# internal marker doc_type for write idempotency — kept in its own namespace so
# a caller-supplied document_ref can never be mistaken for a write marker
DOC_TYPE_IDEMPOTENCY_MARKER = "_idempotency_marker"

_SYNTHETIC_PAYERS = [
    "Synthetic Health Plan", "Acme Mutual (Test)", "Granite Test PPO",
    "Northern Lights HMO (Fictional)",
]


@dataclass
class InsurancePackage:
    package_id: str
    payer_name: str
    member_id_masked: str          # ONLY the masked tail is ever stored
    effective_from: str            # ISO date
    effective_to: Optional[str]    # ISO date; past date ⇒ expired
    status: str = "active"         # "active" | "expired"


@dataclass
class Appointment:
    appointment_id: str
    patient_ref: str
    scheduled_at: str              # ISO datetime


@dataclass
class AttachedDocument:
    document_ref: str
    doc_type: str
    attached_at: str


@dataclass
class PatientRecord:
    patient_ref: str
    display_name: str              # always "Synthetic Patient NNN"
    appointments: list[Appointment] = field(default_factory=list)
    insurance: Optional[InsurancePackage] = None
    documents: list[AttachedDocument] = field(default_factory=list)
    # seeded 271 outcome; flips to ACTIVE after a verified insurance write
    eligibility_outcome: str = ELIGIBILITY_ACTIVE
    # synthetic contact details for the chase worklist — 555 numbers and
    # example.test addresses only, so nothing here can ever reach a real person
    phone: str = ""
    email: str = ""


@dataclass(frozen=True)
class EligibilityResponse:
    patient_ref: str
    transaction: str               # "271"
    status: str                    # ACTIVE | INACTIVE | TIMEOUT
    checked_at: str


class MockAthenaNet:
    """Deterministic synthetic athenaNet. Public surface == future real client."""

    def __init__(self, seed: int = 20260813, now: Optional[datetime] = None) -> None:
        self._rng = random.Random(seed)
        self.now = now or datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)
        self._patients: dict[str, PatientRecord] = {}
        self._package_seq = 0

    # ------------------------------------------------------------------ #
    # world building
    # ------------------------------------------------------------------ #
    def seed_world(self, n_patients: int = 40, horizon_days: int = 14,
                   direct_contract_refs: tuple[str, ...] = ("PT-DC-0001", "PT-DC-0002")) -> None:
        """Build a synthetic practice. Mix, by design:
        ~15% no insurance on file, ~10% expired package; of the insured rest
        ~10% INACTIVE and ~5% TIMEOUT on eligibility. Direct-contract patients
        (matching the bundled F3 list) carry no insurance — the practice does
        not bill insurance for them."""
        rng = self._rng
        for i in range(1, n_patients + 1):
            ref = f"PT-{1000 + i}"
            p = PatientRecord(patient_ref=ref,
                              display_name=f"Synthetic Patient {i:03d}",
                              phone=f"(603) 555-{1000 + i:04d}",
                              email=f"patient{i:03d}@example.test")
            r = rng.random()
            if r < 0.15:
                p.insurance = None                      # never collected
            elif r < 0.25:
                p.insurance = self._make_package(rng, expired=True)
            else:
                p.insurance = self._make_package(rng, expired=False)
                r2 = rng.random()
                if r2 < 0.10:
                    p.eligibility_outcome = ELIGIBILITY_INACTIVE
                elif r2 < 0.15:
                    p.eligibility_outcome = ELIGIBILITY_TIMEOUT
            for a in range(rng.randint(0, 2)):
                offset = timedelta(days=rng.randint(0, horizon_days),
                                   hours=rng.randint(8, 16))
                p.appointments.append(Appointment(
                    appointment_id=f"AP-{ref}-{a + 1}",
                    patient_ref=ref,
                    scheduled_at=(self.now + offset).isoformat(),
                ))
            self._patients[ref] = p

        # direct-contract patients: upcoming appointment, no insurance package
        for j, ref in enumerate(direct_contract_refs, start=1):
            p = PatientRecord(patient_ref=ref,
                              display_name=f"Synthetic DC Patient {j:03d}",
                              phone=f"(603) 555-{9000 + j:04d}",
                              email=f"dcpatient{j:03d}@example.test")
            p.appointments.append(Appointment(
                appointment_id=f"AP-{ref}-1", patient_ref=ref,
                scheduled_at=(self.now + timedelta(days=1, hours=9)).isoformat(),
            ))
            self._patients[ref] = p

    def _make_package(self, rng: random.Random, *, expired: bool) -> InsurancePackage:
        self._package_seq += 1
        masked = f"…{rng.randint(1000, 9999)}"
        if expired:
            start = self.now - timedelta(days=400)
            end = self.now - timedelta(days=rng.randint(5, 90))
        else:
            start = self.now - timedelta(days=200)
            end = self.now + timedelta(days=rng.randint(60, 300))
        return InsurancePackage(
            package_id=f"PKG-{self._package_seq:05d}",
            payer_name=rng.choice(_SYNTHETIC_PAYERS),
            member_id_masked=masked,
            effective_from=start.date().isoformat(),
            effective_to=end.date().isoformat(),
            status="expired" if expired else "active",
        )

    # ------------------------------------------------------------------ #
    # read surface
    # ------------------------------------------------------------------ #
    def list_patients(self) -> list[PatientRecord]:
        return list(self._patients.values())

    def get_patient(self, patient_ref: str) -> PatientRecord:
        return self._patients[patient_ref]

    def check_eligibility(self, patient_ref: str,
                          now: Optional[datetime] = None) -> EligibilityResponse:
        """270/271 pair, mocked: returns the patient's seeded outcome."""
        p = self._patients[patient_ref]
        checked = (now or self.now).isoformat()
        if p.insurance is None:
            status = ELIGIBILITY_INACTIVE
        elif p.insurance.status == "expired":
            status = ELIGIBILITY_INACTIVE
        else:
            status = p.eligibility_outcome
        return EligibilityResponse(patient_ref=patient_ref, transaction="271",
                                   status=status, checked_at=checked)

    # ------------------------------------------------------------------ #
    # write surface (idempotent)
    # ------------------------------------------------------------------ #
    def write_insurance(self, patient_ref: str, *, payer_name: str,
                        member_id_masked: str, idempotency_key: str,
                        now: Optional[datetime] = None) -> dict:
        """Create/replace the patient's insurance package. Idempotent on
        ``idempotency_key`` — a repeat with the same key reports duplicate."""
        p = self._patients[patient_ref]
        key_marker = f"ins-write:{idempotency_key}"
        if any(d.document_ref == key_marker
               and d.doc_type == DOC_TYPE_IDEMPOTENCY_MARKER
               for d in p.documents):
            return {"written": False, "duplicate": True,
                    "package_id": p.insurance.package_id if p.insurance else None}
        ts = (now or self.now)
        self._package_seq += 1
        p.insurance = InsurancePackage(
            package_id=f"PKG-{self._package_seq:05d}",
            payer_name=payer_name,
            member_id_masked=member_id_masked,
            effective_from=ts.date().isoformat(),
            effective_to=(ts + timedelta(days=365)).date().isoformat(),
            status="active",
        )
        p.eligibility_outcome = ELIGIBILITY_ACTIVE   # verified card ⇒ coverage live
        p.documents.append(AttachedDocument(
            document_ref=key_marker, doc_type=DOC_TYPE_IDEMPOTENCY_MARKER,
            attached_at=ts.isoformat()))
        return {"written": True, "duplicate": False,
                "package_id": p.insurance.package_id}

    def attach_document(self, patient_ref: str, *, doc_type: str,
                        document_ref: str,
                        now: Optional[datetime] = None) -> dict:
        p = self._patients[patient_ref]
        if any(d.document_ref == document_ref and d.doc_type == doc_type
               for d in p.documents):
            return {"attached": False, "duplicate": True}
        p.documents.append(AttachedDocument(
            document_ref=document_ref, doc_type=doc_type,
            attached_at=(now or self.now).isoformat()))
        return {"attached": True, "duplicate": False}
