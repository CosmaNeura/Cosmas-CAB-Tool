"""The follow-up chassis — an approval-gated sequence over one chase item.

This is the shared machinery the Aug 12 synthesis identified: a deterministic
detector feeds a work queue, an approval-gated state machine drives the touches,
every contact is logged, and metrics come off the log. F5 (self-pay balances)
becomes a second queue type on this same chassis — different trigger, different
gate semantics, different templates, same state machine.

What the system does and what a person does
-------------------------------------------
The system decides *when* a touch is due, renders it, and — for email, SMS and
portal — sends it. For phone it creates a task, because an automated call about
insurance is a TCPA problem and the ask was for one integrated list, not a
robocall. A human executes the call and logs the outcome and minutes into the
same contact history, which is what makes the phone step visible in the metrics
rather than invisible work.

Nothing sends without an approved policy. In ``PER_POLICY`` mode that approval is
given once; in ``PER_MESSAGE`` mode each touch is also individually released.

Everything that can be pushed to the caller is: the clock, ids, and the transport.
``send`` is an injected callable, so the whole sequence runs end to end in tests
and in the demo with no email or SMS provider anywhere near it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from .models import (
    TERMINAL, Attempt, ChaseItem, Channel, CoverageSnapshot, State,
)
from .policy import ApprovalMode, ChasePolicy


class Transport(Protocol):
    """Sends one rendered message. Returns a delivery outcome string."""

    def __call__(self, *, channel: Channel, patient_ref: str,
                 subject: str, body: str) -> str: ...


def record_only_transport(*, channel: Channel, patient_ref: str,
                          subject: str, body: str) -> str:
    """Default transport: records the send without contacting anyone.

    The default being inert is deliberate. Real messaging is gated on the
    deployment layer and the BAA, so the demo and the tests exercise the whole
    state machine with nothing leaving the building.
    """
    return "sent"


class NotApproved(PermissionError):
    """A send was attempted under a policy no human has approved."""


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SequenceRunner:
    """Advances chase items through their cadence."""
    policy: ChasePolicy
    transport: Transport = record_only_transport
    # A real clock by default. Tests and the demo inject a fixed one; a constant
    # placeholder here would silently stamp every production row with a bogus
    # timestamp and quietly corrupt time-to-card.
    now: Callable[[], str] = _utc_now

    # ---------------------------------------------------------------- release #
    def release(self, item: ChaseItem, *, by: str, reason: str = "") -> ChaseItem:
        """Clear an item to be worked.

        This is the human decision for the items that need one — anything the
        direct-contract gate sent to review. It is also how a per-message
        approval is given.
        """
        if not by.strip():
            raise ValueError("releasing an item requires a named person")
        if item.state in TERMINAL:
            return item
        item.state = State.READY
        item.review_reason = (f"released by {by}" + (f": {reason}" if reason else "")
                              if item.review_reason else item.review_reason)
        item.assignee = item.assignee or by
        return item

    def suppress(self, item: ChaseItem, *, by: str, reason: str) -> ChaseItem:
        """Close an item without contacting the patient (a human's call)."""
        if not reason.strip():
            raise ValueError("suppressing an item requires a reason")
        item.state = State.RESOLVED_OTHER
        item.closed_at = self.now()
        item.resolution = {"outcome": "suppressed", "by": by, "reason": reason}
        return item

    # ------------------------------------------------------------------ due? #
    def next_step(self, item: ChaseItem, snap: CoverageSnapshot):
        """The step that should happen next, or None when the cadence is spent."""
        steps = self.policy.steps_for(snap.reachable_on())
        return steps[item.touches] if item.touches < len(steps) else None

    def is_actionable(self, item: ChaseItem) -> bool:
        """Whether the runner may advance this item unattended."""
        if item.state in TERMINAL:
            return False
        if item.state == State.NEEDS_REVIEW:
            return False          # a person owns this one
        return self.policy.approved

    # ------------------------------------------------------------------- run #
    def advance(
        self,
        item: ChaseItem,
        snap: CoverageSnapshot,
        *,
        released_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Perform at most one touch, or explain why not.

        Returns a small record of what happened, so a caller can log or display
        it without inspecting the item's internals.
        """
        if item.state in TERMINAL:
            return {"action": "none", "reason": f"already {item.state.value}"}
        if item.state == State.NEEDS_REVIEW:
            return {"action": "awaiting_review", "reason": item.review_reason}
        if item.pending_task is not None:
            # A person owes this row a call. Issuing the next touch now would put
            # the same task back on the worklist tomorrow and the one after that,
            # because a task records no attempt until its outcome is logged.
            return {"action": "awaiting_staff_task",
                    "reason": f"waiting on a logged outcome for the "
                              f"{item.pending_task['channel']} task created "
                              f"{item.pending_task['created_at']}",
                    "task": item.pending_task}
        if not self.policy.approved:
            raise NotApproved(
                f"policy '{self.policy.name}' has not been approved by anyone; "
                "no patient will be contacted until a named person approves it.")
        if (self.policy.approval_mode is ApprovalMode.PER_MESSAGE
                and not released_by):
            return {"action": "awaiting_release",
                    "reason": "policy runs in per-message mode; this touch needs "
                              "an explicit release"}

        reachable = snap.reachable_on()
        if not reachable:
            item.state = State.UNREACHABLE
            item.closed_at = self.now()
            item.resolution = {
                "outcome": "unreachable",
                "reason": "no channel with contact details and, where required, "
                          "recorded consent",
            }
            return {"action": "unreachable", "reason": item.resolution["reason"]}

        step = self.next_step(item, snap)
        if step is None:
            # Two different situations reach here, and telling staff the wrong one
            # wastes their time. "Exhausted" means we contacted the patient and
            # they didn't respond. "No usable step" means the policy has nothing
            # we are permitted to send this patient — a policy or consent problem,
            # not a patient problem. Reporting zero touches as an escalation would
            # read as "they ignored us" about messages that never went out.
            if item.touches == 0:
                item.state = State.NEEDS_REVIEW
                item.review_reason = (
                    "the cadence has no step this patient can receive — every "
                    f"configured channel is unavailable for them (reachable on: "
                    f"{', '.join(c.value for c in reachable) or 'nothing'}). "
                    "Add a channel to the policy, or work this row by hand.")
                return {"action": "awaiting_review", "reason": item.review_reason,
                        "touches": 0}
            item.state = State.ESCALATED
            item.closed_at = self.now()
            item.resolution = {
                "outcome": "escalated",
                "reason": f"cadence exhausted after {item.touches} touches "
                          "with no card received",
            }
            return {"action": "escalated", "touches": item.touches}

        # Consent is re-checked at send time, not just at planning time: consent
        # can be withdrawn between touches, and a withdrawn consent must stop the
        # next message rather than the next sequence.
        if self.policy.requires_consent(step.channel) and not snap.consented(step.channel):
            item.state = State.NEEDS_REVIEW
            item.review_reason = (
                f"{step.channel.value} requires recorded consent and none is on "
                "file; a person should choose another channel")
            return {"action": "awaiting_review", "reason": item.review_reason}

        template = self.policy.template(step.template_id)
        rendered = template.render(
            practice=self.policy.practice_name,
            upload_url=self.policy.upload_url,
        )

        at = self.now()
        if self.policy.is_staff_task(step.channel):
            # A task, not a send. It is recorded as pending so the row stops
            # advancing until a person logs the outcome — the cadence resumes
            # from their entry, not from a guess about what happened.
            item.state = State.IN_PROGRESS
            item.pending_task = {
                "channel": step.channel.value,
                "template_id": template.template_id,
                "script": rendered["body"],
                "created_at": at,
                "assignee": item.assignee or None,
            }
            return {
                "action": "staff_task",
                **item.pending_task,
                "reason": "phone contact is performed by a person, not the system",
            }

        outcome = self.transport(
            channel=step.channel, patient_ref=item.patient_ref,
            subject=rendered["subject"], body=rendered["body"])
        item.attempts.append(Attempt(
            channel=step.channel, template_id=template.template_id, at=at,
            actor="system", outcome=outcome))
        item.state = State.IN_PROGRESS
        return {
            "action": "sent", "channel": step.channel.value,
            "template_id": template.template_id, "outcome": outcome,
            "touches": item.touches,
        }

    # ------------------------------------------------------- staff reporting #
    def log_staff_attempt(
        self, item: ChaseItem, *, channel: Channel, template_id: str,
        actor: str, outcome: str, minutes_spent: float = 0.0, note: str = "",
    ) -> ChaseItem:
        """Record a touch a person made — the phone step, or an ad-hoc contact.

        Minutes are the input to the staff-minutes-per-card metric, which is the
        number that shows whether this tool actually took work off the two people
        who do this job.
        """
        if not actor.strip():
            raise ValueError("a staff attempt must record who made it")
        item.attempts.append(Attempt(
            channel=channel, template_id=template_id, at=self.now(), actor=actor,
            outcome=outcome, minutes_spent=minutes_spent, note=note))
        # Logging the outcome closes any task that was waiting on it, which is
        # what lets the cadence move on.
        if item.pending_task and item.pending_task["channel"] == channel.value:
            item.pending_task = None
        if item.state not in TERMINAL:
            item.state = State.IN_PROGRESS
        return item

    # ------------------------------------------------------------ completion #
    def mark_submitted(self, item: ChaseItem, *, note: str = "") -> ChaseItem:
        """The patient sent something. Not closed — capture must still verify it."""
        if item.state in TERMINAL:
            return item
        item.state = State.SUBMITTED
        item.resolution = {"outcome": "submitted", "note": note}
        return item

    def close_verified(
        self, item: ChaseItem, *, verified_by: str, athena_write_id: str = "",
        eligibility_status: str = "",
    ) -> ChaseItem:
        """F9c: close on a *verified* card, never on a submission.

        The distinction is the whole completion loop. A patient uploading a photo
        is not a card on file; staff verification and the write into the right
        Athena encounter are what finish the job, and only that closes the row.
        """
        if not verified_by.strip():
            raise ValueError("closing a chase requires the verifier's identity")
        item.state = State.VERIFIED_CLOSED
        item.closed_at = self.now()
        item.resolution = {
            "outcome": "verified_closed",
            "verified_by": verified_by,
            "athena_write_id": athena_write_id or None,
            "eligibility_status": eligibility_status or None,
        }
        return item
