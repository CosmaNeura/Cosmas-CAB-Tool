"""The shared follow-up chassis (synthesis §F5/§F9b): deterministic detector →
work queue → approval-gated sequence state machine → contact log → metrics.

Two queue types run on the same chassis and differ only in trigger, gate
semantics, and templates:

  * ``card_chase`` (F9): direct-contract matches are routed to a human-review
    bucket at detection (see detector.py) — policy question lands Aug 13.
  * ``self_pay`` (F5): F3 is **blocking** — a direct-contract patient can
    never enter the queue at all; enqueue refuses.

Rails enforced in code, not convention:
  * every outbound message requires explicit staff approval first;
  * message bodies carry a CardCapture submission link and NEVER any insurance
    or card data — ``render_message`` refuses context keys that look like
    payload, and tests scan rendered bodies;
  * every attempt is logged (channel, timestamp, template id, approver).

Cadence, tone, templates, channel/consent policy are Aug 13 inputs — they ship
here as **configuration** (``ChaseConfig``), not code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..guards import PracticeListSource, check_direct_contract
from .card_capture import submission_link
from .detector import ChaseItem, STATE_HUMAN_REVIEW, STATE_OPEN


class SequenceError(RuntimeError):
    """An action violated the sequence rails (unapproved send, bad transition,
    or a blocked patient entering a queue)."""


class QueueType:
    CARD_CHASE = "card_chase"
    SELF_PAY = "self_pay"


# sequence_state graph (synthesis §F9b)
STATE_APPROVED = "staff_approved"
STATE_SENT = "sent"
STATE_REMINDED = "reminded"
STATE_SUBMITTED = "submitted"
STATE_CLOSED = "verified_closed"
STATE_ESCALATED = "escalated_to_staff"
STATE_UNREACHABLE = "unreachable"

_TRANSITIONS: dict[str, set[str]] = {
    # a patient may submit before any outreach — the item still auto-closes
    STATE_OPEN: {STATE_APPROVED, STATE_SUBMITTED, STATE_ESCALATED,
                 STATE_UNREACHABLE},
    STATE_HUMAN_REVIEW: {STATE_OPEN, STATE_ESCALATED},   # staff decision only
    STATE_APPROVED: {STATE_SENT, STATE_SUBMITTED, STATE_ESCALATED},
    STATE_SENT: {STATE_REMINDED, STATE_SUBMITTED, STATE_CLOSED,
                 STATE_ESCALATED, STATE_UNREACHABLE},
    STATE_REMINDED: {STATE_REMINDED, STATE_SUBMITTED, STATE_CLOSED,
                     STATE_ESCALATED, STATE_UNREACHABLE},
    STATE_SUBMITTED: {STATE_CLOSED, STATE_ESCALATED},
}

# terminal states never transition
_TERMINAL = {STATE_CLOSED, STATE_ESCALATED, STATE_UNREACHABLE}

# context keys allowed into a message template — nothing payload-shaped
_ALLOWED_TEMPLATE_KEYS = {"display_name", "practice_name", "link", "appointment_date"}
_FORBIDDEN_BODY_FRAGMENTS = ("member id", "member_id", "policy number", "card number")


@dataclass
class ChaseConfig:
    """Aug-13-pending knobs live here as configuration, not code."""
    practice_name: str = "Demo Practice (Synthetic)"
    card_capture_base_url: str = "https://cardcapture.example.test/submit"
    channels: tuple[str, ...] = ("email",)         # consent policy: Aug 13 (§8 Q10)
    max_reminders: int = 2                          # cadence: Aug 13
    templates: dict = field(default_factory=lambda: {
        "card_request.v0": (
            "Hi {display_name}, ahead of your upcoming visit {practice_name} "
            "needs a current copy of your insurance card. Please use this "
            "secure link to submit it: {link} — it takes under a minute. "
            "Thank you!"
        ),
        "card_reminder.v0": (
            "Hi {display_name}, a quick reminder from {practice_name}: we "
            "still need your current insurance card before your visit. "
            "Secure link: {link}"
        ),
    })


class ChaseQueue:
    """Approval-gated work queue over ChaseItems. One instance per queue type."""

    def __init__(self, queue_type: str, config: Optional[ChaseConfig] = None,
                 list_source: Optional[PracticeListSource] = None) -> None:
        self.queue_type = queue_type
        self.config = config or ChaseConfig()
        self._list_source = list_source
        self.items: dict[str, ChaseItem] = {}
        self.staff_actions: list[dict] = []          # instrumented from day one

    # ------------------------------------------------------------------ #
    # intake
    # ------------------------------------------------------------------ #
    def enqueue(self, item: ChaseItem, *, now: datetime) -> ChaseItem:
        item.queue_type = self.queue_type
        if self.queue_type == QueueType.SELF_PAY:
            # F3 is BLOCKING here: a direct-contract patient can never enter
            # a self-pay chase sequence. Re-checked at the door regardless of
            # what the caller claims.
            gate = check_direct_contract(item.patient_ref,
                                         self._list_source, now=now)
            if gate.blocked:
                raise SequenceError(
                    f"direct-contract gate: {item.patient_ref} cannot enter a "
                    f"self-pay sequence (status={gate.status})")
        self.items[item.chase_id] = item
        return item

    # ------------------------------------------------------------------ #
    # sequence actions (every one is a staff action, logged)
    # ------------------------------------------------------------------ #
    def approve(self, chase_id: str, *, staff_id: str, now: datetime) -> ChaseItem:
        if not staff_id:
            raise SequenceError("approval requires a staff_id")
        item = self._transition(chase_id, STATE_APPROVED)
        self._log_staff(now, staff_id, "approve", chase_id)
        return item

    def send(self, chase_id: str, *, template_id: str, channel: str = "email",
             now: datetime, sent_by: str) -> str:
        """Send the (simulated) message. Requires prior staff approval —
        enforced by the transition table: only staff_approved may move to
        sent. Returns the rendered body so callers/tests can inspect it."""
        item = self.items[chase_id]
        if channel not in self.config.channels:
            raise SequenceError(f"channel {channel!r} not enabled (consent "
                                "policy is an Aug 13 input)")
        first_send = item.sequence_state == STATE_APPROVED
        if not first_send:
            if item.sequence_state not in (STATE_SENT, STATE_REMINDED):
                raise SequenceError(
                    f"send requires staff approval first (state="
                    f"{item.sequence_state})")
            reminders = sum(1 for a in item.attempts
                            if a["template_id"].startswith("card_reminder"))
            if reminders >= self.config.max_reminders:
                raise SequenceError("max reminders reached — escalate to staff")
        body = self.render_message(template_id, item)
        self._transition(chase_id, STATE_SENT if first_send else STATE_REMINDED)
        item.attempts.append({
            "channel": channel, "template_id": template_id,
            "sent_at": now.isoformat(), "approved_by": sent_by,
        })
        self._log_staff(now, sent_by, "send", chase_id)
        return body

    def patient_submitted(self, chase_id: str, *, now: datetime) -> ChaseItem:
        item = self._transition(chase_id, STATE_SUBMITTED)
        return item

    def close_verified(self, chase_id: str, *, resolution: dict,
                       now: datetime) -> ChaseItem:
        item = self._transition(chase_id, STATE_CLOSED)
        item.resolution = dict(resolution, closed_at=now.isoformat())
        return item

    def escalate(self, chase_id: str, *, staff_id: str, now: datetime,
                 reason: str) -> ChaseItem:
        item = self._transition(chase_id, STATE_ESCALATED)
        item.resolution = {"escalated": True, "reason": reason,
                           "closed_at": now.isoformat()}
        self._log_staff(now, staff_id, "escalate", chase_id)
        return item

    def review_release(self, chase_id: str, *, staff_id: str,
                       now: datetime, reason: str) -> ChaseItem:
        """Staff decision that a human-review item may enter the normal
        sequence (e.g., Aug 13 answers the direct-contract chase policy)."""
        item = self.items[chase_id]
        if item.sequence_state != STATE_HUMAN_REVIEW:
            raise SequenceError("only human_review items can be released")
        if not reason.strip():
            raise SequenceError("review release requires a reason")
        item = self._transition(chase_id, STATE_OPEN)
        self._log_staff(now, staff_id, f"review_release: {reason}", chase_id)
        return item

    # ------------------------------------------------------------------ #
    # message rendering — structurally unable to carry card data
    # ------------------------------------------------------------------ #
    def render_message(self, template_id: str, item: ChaseItem,
                       display_name: str = "there") -> str:
        template = self.config.templates.get(template_id)
        if template is None:
            raise SequenceError(f"unknown template {template_id!r}")
        context = {
            "display_name": display_name,
            "practice_name": self.config.practice_name,
            "link": submission_link(self.config.card_capture_base_url,
                                    item.patient_ref),
            "appointment_date": (item.appointment_at or "")[:10],
        }
        assert set(context) <= _ALLOWED_TEMPLATE_KEYS
        body = template.format(**context)
        lowered = body.lower()
        for frag in _FORBIDDEN_BODY_FRAGMENTS:
            if frag in lowered:
                raise SequenceError(
                    f"template {template_id!r} mentions {frag!r} — outbound "
                    "messages never carry insurance/card data")
        return body

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _transition(self, chase_id: str, new_state: str) -> ChaseItem:
        item = self.items[chase_id]
        cur = item.sequence_state
        if cur in _TERMINAL:
            raise SequenceError(f"{chase_id} is terminal ({cur})")
        if new_state not in _TRANSITIONS.get(cur, set()):
            raise SequenceError(f"illegal transition {cur} -> {new_state}")
        item.sequence_state = new_state
        return item

    def _log_staff(self, now: datetime, staff_id: str, action: str,
                   chase_id: str) -> None:
        self.staff_actions.append({"at": now.isoformat(), "staff_id": staff_id,
                                   "action": action, "chase_id": chase_id})
