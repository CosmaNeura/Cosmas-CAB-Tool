"""Chase policy — cadence, channels and templates as configuration.

Everything in this module is a value the practice sets, not behaviour in code.
That is deliberate: cadence, tone, templates, channel order and consent policy
are all Aug 12 open questions (§8 Q3, Q10) that the Aug 13 follow-up did not
answer. Shipping them as configuration means the interview with the two staff who
do this job changes a config file, not a codebase.

The human-in-the-loop decision
------------------------------
"HITL but automated" has two readings, and they are not the same system:

* ``ApprovalMode.PER_POLICY`` — a named person approves the *policy* once, and
  the system then executes its touches on schedule, escalating anything it cannot
  handle. This is what removes work: at 300–500 held claims a month, a queue that
  needs a click per message is a queue nobody drains.
* ``ApprovalMode.PER_MESSAGE`` — every outbound touch is approved individually.
  Safer, slower, and what the Aug 12 spec assumed.

Both are supported and the choice is one field. What is *not* configurable: an
unapproved policy sends nothing, ever, in either mode. There is always a named
human behind any patient contact — the question is only how often they are asked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models import REQUIRES_CONSENT, STAFF_TASK, SYSTEM_SENDS, Channel

# Terms that must never appear in an outbound message body. The message tells the
# patient we need their insurance information and links them to the secure upload
# page; it never contains insurance data. Enforced by `assert_no_phi`, because a
# convention nobody checks is not a control.
_FORBIDDEN_IN_BODY = (
    "member id", "member number", "policy number", "policy #", "group number",
    "group #", "subscriber id", "rx bin", "payer id", "medicaid id",
    "medicare number", "mbi", "ssn", "social security",
)


class ApprovalMode(str, Enum):
    PER_POLICY = "per_policy"
    PER_MESSAGE = "per_message"


class MessageContainsPHI(ValueError):
    """A template or rendered body looks like it carries insurance data."""


@dataclass(frozen=True)
class Template:
    template_id: str
    channel: Channel
    subject: str
    body: str

    def render(self, *, practice: str, upload_url: str,
               patient_first_name: str = "") -> dict[str, str]:
        """Fill the template. Only these three substitutions exist.

        The narrow substitution set is the point: a template cannot interpolate a
        member id because there is no placeholder for one.
        """
        values = {
            "practice": practice,
            "upload_url": upload_url,
            "name": patient_first_name or "there",
        }
        rendered = {
            "subject": self.subject.format(**values),
            "body": self.body.format(**values),
        }
        assert_no_phi(rendered["body"])
        assert_no_phi(rendered["subject"])
        return rendered


def assert_no_phi(text: str) -> None:
    lowered = (text or "").lower()
    for term in _FORBIDDEN_IN_BODY:
        if term in lowered:
            raise MessageContainsPHI(
                f"outbound message body mentions {term!r}. Chase messages ask the "
                "patient for their information and link to the secure upload page; "
                "they never contain insurance data.")


# Defaults, written to be replaced. Deliberately plain — tone is the staff's call.
DEFAULT_TEMPLATES: tuple[Template, ...] = (
    Template("email.first", Channel.EMAIL,
             "{practice}: we need your insurance card",
             "Hi {name},\n\nWe're missing current insurance information for your "
             "upcoming visit with {practice}. You can add it securely here:\n"
             "{upload_url}\n\nIt takes about a minute, and it means we can bill "
             "your insurer instead of sending you a bill.\n\nThank you."),
    Template("sms.first", Channel.SMS, "",
             "{practice}: we're missing your insurance info for your upcoming "
             "visit. Add it securely here: {upload_url} Reply STOP to opt out."),
    Template("portal.first", Channel.PORTAL,
             "Insurance information needed",
             "We're missing current insurance information for your upcoming visit. "
             "Please add it here: {upload_url}"),
    Template("email.reminder", Channel.EMAIL,
             "{practice}: reminder about your insurance card",
             "Hi {name},\n\nA quick reminder that we still need your insurance "
             "information:\n{upload_url}\n\nIf your coverage has changed or you'd "
             "rather give it over the phone, just reply and we'll call you."),
    Template("sms.reminder", Channel.SMS, "",
             "{practice}: reminder — we still need your insurance info: "
             "{upload_url} Reply STOP to opt out."),
    # The phone step is a task for a person; the "body" is their call script.
    Template("phone.call", Channel.PHONE, "",
             "Call {name} about missing insurance information for their upcoming "
             "visit. Offer to take the details over the phone, or resend the "
             "secure link: {upload_url}. Log the outcome and minutes spent."),
)


@dataclass(frozen=True)
class Step:
    """One rung of the cadence."""
    channel: Channel
    template_id: str
    # Absolute local calendar days after detection when this step becomes due.
    day_offset: int


@dataclass
class ChasePolicy:
    """The practice's chase configuration, and the record of who approved it."""
    name: str = "insurance-chase.v1"
    practice_name: str = "your care team"
    upload_url: str = "https://example.invalid/insurance"
    approval_mode: ApprovalMode = ApprovalMode.PER_POLICY
    # Quiet hours and business-day handling are scheduling concerns the staff
    # will have opinions about; recorded here so they are visible in the demo.
    quiet_hours: tuple[int, int] = (21, 8)
    max_touches: int = 4
    steps: tuple[Step, ...] = (
        Step(Channel.EMAIL, "email.first", 0),
        Step(Channel.SMS, "sms.first", 1),
        Step(Channel.EMAIL, "email.reminder", 3),
        Step(Channel.PHONE, "phone.call", 5),
    )
    templates: tuple[Template, ...] = DEFAULT_TEMPLATES
    approved_by: str = ""
    approved_at: str = ""
    _index: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._index = {t.template_id: t for t in self.templates}
        for template in self.templates:
            # Fail at configuration time, not at send time.
            assert_no_phi(template.body)
            assert_no_phi(template.subject)

    @property
    def approved(self) -> bool:
        return bool(self.approved_by and self.approved_at)

    def approve(self, *, by: str, at: str) -> "ChasePolicy":
        if not by.strip():
            raise ValueError("policy approval needs a named approver")
        self.approved_by, self.approved_at = by, at
        return self

    def template(self, template_id: str) -> Template:
        try:
            return self._index[template_id]
        except KeyError as e:
            raise KeyError(f"policy '{self.name}' has no template '{template_id}'") from e

    def steps_for(self, channels: list[Channel]) -> list[Step]:
        """The cadence trimmed to channels this patient is actually reachable on.

        A step the patient cannot receive is dropped rather than counted as a
        touch — otherwise a patient with no mobile number "gets" an SMS step and
        the sequence exhausts itself without ever reaching them.
        """
        return [s for s in self.steps if s.channel in channels][: self.max_touches]

    def requires_consent(self, channel: Channel) -> bool:
        return channel in REQUIRES_CONSENT

    def is_system_send(self, channel: Channel) -> bool:
        return channel in SYSTEM_SENDS

    def is_staff_task(self, channel: Channel) -> bool:
        return channel in STAFF_TASK

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "approval_mode": self.approval_mode.value,
            "approved": self.approved,
            "approved_by": self.approved_by or None,
            "approved_at": self.approved_at or None,
            "max_touches": self.max_touches,
            "quiet_hours": list(self.quiet_hours),
            "steps": [{"channel": s.channel.value, "template_id": s.template_id,
                       "day_offset": s.day_offset,
                       "performed_by": "system" if self.is_system_send(s.channel)
                                       else "staff"}
                      for s in self.steps],
        }
