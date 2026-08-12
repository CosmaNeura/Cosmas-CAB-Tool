"""CardCapture seam — the interface the upstream capture pipeline implements.

Confirmed Aug 12: no prior CardCapture implementation exists to import (the
workspace that produced it was reset), so this module IS the canonical
interface going forward. The upstream pipeline (patient upload page →
extraction → staff verification UI) is future work; it will emit
``CardVerifiedEvent`` and consume ``submission_link``, and nothing else in
the chase package changes when it lands.

Nothing here ever holds a full member ID or card image — the verified event
carries only the payer name and a masked tail, mirroring the real pipeline's
staff-verified output.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


def submission_link(base_url: str, patient_ref: str) -> str:
    """Deterministic, opaque, PHI-free submission link. The token is a hash of
    the synthetic patient ref — enough for the demo; the real CardCapture link
    issuance replaces this."""
    token = hashlib.sha256(f"cardcapture:{patient_ref}".encode()).hexdigest()[:16]
    return f"{base_url}/{token}"


@dataclass(frozen=True)
class CardVerifiedEvent:
    """What CardCapture emits after staff verification passes."""
    patient_ref: str
    payer_name: str
    member_id_masked: str        # masked tail only, e.g. "…4821"
    card_document_ref: str       # idempotency key for the Athena write
    verified_by: str             # staff member who verified the extraction
    verified_at: str


def simulate_patient_submission(patient_ref: str, *, now: datetime,
                                verified_by: str = "staff-demo") -> CardVerifiedEvent:
    """Demo stand-in: the patient followed the link, uploaded a synthetic
    card, and staff verified the extraction (human verification before
    persistence — CLAUDE.md constraint #8)."""
    doc_ref = f"CARD-{hashlib.sha256(patient_ref.encode()).hexdigest()[:10]}"
    return CardVerifiedEvent(
        patient_ref=patient_ref,
        payer_name="Synthetic Health Plan",
        member_id_masked="…4821",
        card_document_ref=doc_ref,
        verified_by=verified_by,
        verified_at=now.isoformat(),
    )
