"""F9 — the biller-facing Card Chase API (mounts onto the engine app).

The worklist the billing team asked for (Grace 8/6): the system collates the
patients whose insurance data is missing, expired, or inactive; staff reach
out by phone or email from the list; card info arrives either through the
CardCapture link or typed in by staff during a call ("could be OCR, could be
manual"); the verified card writes to (mock) athenaNet and the queue item
closes itself.

Everything here operates on one in-memory synthetic world (MockAthenaNet +
ChaseQueue) — zero PHI, zero credentials, reset on process restart. The
manual-entry path accepts only the member ID's LAST FOUR digits, so a full
member number can never transit or be stored even in the demo.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .athena_client import MockAthenaNet
from .chase import (
    ChaseQueue, QueueType, SequenceError, detect, on_card_verified,
    submission_link, summarize,
)
from .chase.card_capture import CardVerifiedEvent


class ChaseWorld:
    """The demo practice: seeded mock athenaNet + one card-chase queue."""

    def __init__(self, seed: int = 20260813, n_patients: int = 40) -> None:
        self.athena = MockAthenaNet(seed=seed, now=datetime.now(timezone.utc))
        self.athena.seed_world(n_patients=n_patients)
        self.queue = ChaseQueue(QueueType.CARD_CHASE)
        self.redetect()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def redetect(self) -> int:
        """Run the detector and stage the results. Enqueue is first-write-wins,
        so re-running never disturbs in-flight or resolved items."""
        now = self.now()
        items = detect(self.athena, now=now)
        for item in items:
            self.queue.enqueue(item, now=now)
        return len(items)

    # ------------------------------------------------------------------ #
    def item_json(self, chase_id: str) -> dict[str, Any]:
        item = self.queue.items[chase_id]
        patient = self.athena.get_patient(item.patient_ref)
        ins = patient.insurance
        return {
            **item.to_json(),
            "display_name": patient.display_name,
            "phone": patient.phone,
            "email": patient.email,
            "submission_link": submission_link(
                self.queue.config.card_capture_base_url, item.patient_ref),
            "insurance": ({"package_id": ins.package_id,
                           "payer_name": ins.payer_name,
                           "member_id_masked": ins.member_id_masked,
                           "status": ins.status,
                           "effective_to": ins.effective_to}
                          if ins else None),
        }

    def queue_json(self) -> dict[str, Any]:
        order = {"open": 0, "staff_approved": 1, "sent": 2, "reminded": 3,
                 "submitted": 4, "human_review": 5, "escalated_to_staff": 6,
                 "unreachable": 7, "verified_closed": 8}
        prio = {"P0": 0, "P1": 1, "P2": 2}
        items = sorted(
            self.queue.items.values(),
            key=lambda i: (order.get(i.sequence_state, 9),
                           prio.get(i.priority, 9), i.appointment_at or "9999"),
        )
        return {
            "items": [self.item_json(i.chase_id) for i in items],
            "metrics": summarize(self.queue.items.values(),
                                 self.queue.staff_actions,
                                 self.queue.machine_actions),
            "outreach_mode": self.queue.config.outreach_mode,
        }


_WORLD: Optional[ChaseWorld] = None


def get_world() -> ChaseWorld:
    global _WORLD
    if _WORLD is None:
        _WORLD = ChaseWorld()
    return _WORLD


# --------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------- #
class StaffAction(BaseModel):
    staff_id: str = Field(min_length=1, max_length=80)
    reason: Optional[str] = Field(default=None, max_length=500)


class ManualCardEntry(BaseModel):
    """Card info taken over the phone — staff is the verifier. Only the last
    four digits of the member ID are accepted, ever."""
    staff_id: str = Field(min_length=1, max_length=80)
    payer_name: str = Field(min_length=2, max_length=120)
    member_id_last4: str = Field(min_length=2, max_length=4)


def _http(exc: SequenceError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


# --------------------------------------------------------------------- #
# route registration (called from api.py)
# --------------------------------------------------------------------- #
def register(app, dependencies) -> None:
    @app.get("/api/chase/queue")
    def chase_queue() -> dict[str, Any]:
        world = get_world()
        world.redetect()               # safe: first-write-wins intake
        return world.queue_json()

    @app.post("/api/chase/{chase_id}/approve", dependencies=dependencies)
    def chase_approve(chase_id: str, body: StaffAction) -> dict[str, Any]:
        world = get_world()
        try:
            world.queue.approve(chase_id, staff_id=body.staff_id,
                                now=world.now())
        except SequenceError as exc:
            raise _http(exc)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown chase item")
        return world.item_json(chase_id)

    @app.post("/api/chase/{chase_id}/send", dependencies=dependencies)
    def chase_send(chase_id: str, body: StaffAction) -> dict[str, Any]:
        world = get_world()
        try:
            rendered = world.queue.send(
                chase_id, template_id="card_request.v0", now=world.now(),
                sent_by=body.staff_id)
        except SequenceError as exc:
            raise _http(exc)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown chase item")
        return {"item": world.item_json(chase_id), "message_body": rendered}

    @app.post("/api/chase/{chase_id}/manual-entry", dependencies=dependencies)
    def chase_manual_entry(chase_id: str, body: ManualCardEntry) -> dict[str, Any]:
        """Staff took the card info during a call: staff IS the human verifier
        (CLAUDE.md constraint #8), the masked entry writes to mock Athena, and
        the item auto-closes — same completion loop as a CardCapture upload."""
        if not re.fullmatch(r"\d{2,4}", body.member_id_last4):
            raise HTTPException(status_code=422,
                                detail="member_id_last4 must be 2-4 digits")
        world = get_world()
        item = world.queue.items.get(chase_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown chase item")
        now = world.now()
        event = CardVerifiedEvent(
            patient_ref=item.patient_ref,
            payer_name=body.payer_name.strip(),
            member_id_masked=f"…{body.member_id_last4}",
            card_document_ref=f"MANUAL-{chase_id}",
            verified_by=body.staff_id,
            verified_at=now.isoformat(),
        )
        result = on_card_verified(world.queue, world.athena, event, now=now)
        return {"item": world.item_json(chase_id), "write": result}

    @app.post("/api/chase/{chase_id}/simulate-submission", dependencies=dependencies)
    def chase_simulate_submission(chase_id: str, body: StaffAction) -> dict[str, Any]:
        """Demo affordance: the patient followed the CardCapture link and the
        upload verified. Clearly a simulation — real uploads arrive from the
        CardCapture pipeline, not from this endpoint."""
        from .chase import simulate_patient_submission
        world = get_world()
        item = world.queue.items.get(chase_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown chase item")
        now = world.now()
        event = simulate_patient_submission(item.patient_ref, now=now,
                                            verified_by=body.staff_id)
        result = on_card_verified(world.queue, world.athena, event, now=now)
        return {"item": world.item_json(chase_id), "write": result}

    @app.post("/api/chase/{chase_id}/escalate", dependencies=dependencies)
    def chase_escalate(chase_id: str, body: StaffAction) -> dict[str, Any]:
        world = get_world()
        try:
            world.queue.escalate(chase_id, staff_id=body.staff_id,
                                 now=world.now(),
                                 reason=body.reason or "escalated from worklist")
        except SequenceError as exc:
            raise _http(exc)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown chase item")
        return world.item_json(chase_id)

    @app.post("/api/chase/{chase_id}/unreachable", dependencies=dependencies)
    def chase_unreachable(chase_id: str, body: StaffAction) -> dict[str, Any]:
        world = get_world()
        if not (body.reason or "").strip():
            raise HTTPException(status_code=422,
                                detail="marking unreachable requires a reason")
        try:
            world.queue.mark_unreachable(chase_id, staff_id=body.staff_id,
                                         now=world.now(), reason=body.reason)
        except SequenceError as exc:
            raise _http(exc)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown chase item")
        return world.item_json(chase_id)
