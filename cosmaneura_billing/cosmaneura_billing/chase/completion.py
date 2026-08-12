"""F9c — the completion loop. A staff-verified card closes the chase.

verified CardCapture event → idempotent Athena insurance write +
``ADMIN_INSURANCECARD`` attach → eligibility re-check → the matching queue
item auto-closes with resolution metadata. Metrics read the closed items.
"""
from __future__ import annotations

from datetime import datetime

from ..athena_client import DOC_TYPE_INSURANCE_CARD, ELIGIBILITY_ACTIVE, MockAthenaNet
from .card_capture import CardVerifiedEvent
from .chassis import ChaseQueue, STATE_CLOSED
from .detector import STATE_HUMAN_REVIEW


def on_card_verified(queue: ChaseQueue, athena: MockAthenaNet,
                     event: CardVerifiedEvent, *, now: datetime) -> dict:
    """Handle a verified card. Idempotent end to end: replaying the same event
    reports duplicate=True and closes nothing twice."""
    write = athena.write_insurance(
        event.patient_ref,
        payer_name=event.payer_name,
        member_id_masked=event.member_id_masked,
        idempotency_key=event.card_document_ref,
        now=now,
    )
    athena.attach_document(event.patient_ref,
                           doc_type=DOC_TYPE_INSURANCE_CARD,
                           document_ref=event.card_document_ref, now=now)
    eligibility = athena.check_eligibility(event.patient_ref, now=now)

    closed = []
    for item in queue.items.values():
        if item.patient_ref != event.patient_ref:
            continue
        if item.sequence_state in (STATE_CLOSED, STATE_HUMAN_REVIEW):
            continue   # review-bucket items stay parked for the staff decision
        # walk the item to closed through legal transitions (a submission may
        # arrive before any outreach — open/approved items close too)
        if item.sequence_state in ("open", "staff_approved", "sent", "reminded"):
            queue.patient_submitted(item.chase_id, now=now)
        if item.sequence_state == "submitted":
            queue.close_verified(item.chase_id, now=now, resolution={
                "card_document_ref": event.card_document_ref,
                "package_id": write.get("package_id"),
                "eligibility_confirmed": eligibility.status == ELIGIBILITY_ACTIVE,
                "verified_by": event.verified_by,
            })
            closed.append(item.chase_id)

    return {
        "written": write["written"], "duplicate": write["duplicate"],
        "eligibility": eligibility.status, "closed_items": closed,
    }
