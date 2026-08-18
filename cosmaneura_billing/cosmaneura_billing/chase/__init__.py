"""Insurance-data chase (F9) — the billers' #1 stated pain.

Grace Targonski, 2026-08-06: *"I would be ecstatic about the cards… It's my
mantra."* 300–500 claims held monthly for missing insurance information.

The validated finding is that the bottleneck is **patient submission, not OCR**.
Card capture already works; what was missing is the system that gets the card to
capture — a master worklist, worked across email, SMS, portal and phone, by the
staff whose whole job this is.

    detector.detect_all   →  worklist.Worklist  →  sequence.SequenceRunner
    (deterministic rules)    (one integrated list)  (approval-gated touches)
                                      ↓
                              metrics.summarize

Guarantees the pieces enforce between them:

* No patient is contacted under a policy no named human approved.
* A direct-contract patient (or one whose status is unknown) never enters an
  automated sequence — the row goes to a person instead of disappearing.
* No outbound message body contains insurance data.
* SMS and phone require recorded consent, re-checked at each touch.
* Phone is a staff task, never an automated call.
* A row closes only on a *verified* card, never on a patient's submission.
"""
from .detector import detect, detect_all, priority_for
from .acs_email import ACSEmailTransport, EmailSubmissionError
from .metrics import baseline_comparison, summarize
from .models import (
    Attempt, ChaseItem, Channel, CoverageSnapshot, State, Trigger,
)
from .policy import ApprovalMode, ChasePolicy, MessageContainsPHI, Step, Template
from .sequence import NotApproved, SequenceRunner
from .store import (
    ChaseStore, PostgresChaseStore, chase_store_from_env,
    chase_store_from_settings,
)
from .worklist import Worklist

__all__ = [
    "ACSEmailTransport", "Attempt", "ApprovalMode", "ChaseItem", "ChasePolicy", "Channel",
    "ChaseStore", "CoverageSnapshot", "EmailSubmissionError",
    "MessageContainsPHI", "NotApproved",
    "PostgresChaseStore", "SequenceRunner", "State", "Step", "Template",
    "Trigger", "Worklist", "chase_store_from_env", "chase_store_from_settings",
    "baseline_comparison", "detect", "detect_all", "priority_for", "summarize",
]
