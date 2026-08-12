"""Feedback agent -- the loop back into the rest of the platform.

This is the "subtle nudges" surface. Its hard rule: every nudge is grounded in
either documentation completeness or the patient's clinical benefit. It will not
emit anything that steers a patient toward additional billable services, or that
asks a provider to do/order/document things for revenue rather than for care.
That rule is enforced by ``_assert_patient_benefit`` -- a feedback event without
a stated patient/clinical benefit (other than pure billing mechanics aimed at
the biller) is dropped.
"""
from __future__ import annotations

from ..schemas import (
    CodeKind, CodeSuggestion, Disposition, FeedbackEvent, FeedbackKind, FeedbackTarget,
)


def _level(code: str | None) -> int:
    return int(code[-1]) if code and code[-1:].isdigit() else 0


class FeedbackAgent:
    def run(
        self,
        facts: dict,
        gaps: list[str],
        suggestions: list[CodeSuggestion],
        *,
        submitted_em_code: str | None,
    ) -> list[FeedbackEvent]:
        events: list[FeedbackEvent] = []

        # 1) Documentation gaps -> provider (completeness, not "bill it anyway")
        for g in gaps:
            events.append(FeedbackEvent(
                kind=FeedbackKind.DOC_GAP, target=FeedbackTarget.PROVIDER, message=g,
                patient_benefit="Complete documentation reflects the care actually delivered.",
            ))

        # 2) Care-quality follow-up -> provider, framed around the patient
        risk = facts.get("risk", {})
        if any("colonoscop" in d.lower() for d in risk.get("drivers", [])):
            events.append(FeedbackEvent(
                kind=FeedbackKind.CARE_QUALITY, target=FeedbackTarget.PROVIDER,
                message="Rectal bleeding in this patient warrants completing the colonoscopy you "
                        "ordered. Consider flagging the patient for navigation/follow-up so it isn't lost.",
                patient_benefit="Timely completion of an indicated diagnostic study; closes the loop on bleeding.",
            ))

        # 3) Intake completeness -> the intake agent (improves the next history)
        problem_names = " ".join(p.get("name", "").lower() for p in facts.get("problems", []))
        if "bleed" in problem_names or "hematochezia" in problem_names:
            events.append(FeedbackEvent(
                kind=FeedbackKind.INTAKE_COMPLETENESS, target=FeedbackTarget.INTAKE_AGENT,
                message="For GI-bleeding presentations, intake should capture: NSAID/aspirin/anticoagulant "
                        "use, stool color & duration, and weight change. These complete the clinical picture.",
                tone="informational",
                patient_benefit="A fuller history at intake means fewer call-backs and a safer workup.",
            ))

        # 4) Coding education -> provider, ONLY about accurately capturing work
        #    that was documented (under-coding correction), never about doing more.
        em = next((s for s in suggestions if s.kind == CodeKind.EM
                   and s.disposition == Disposition.CAPTURED), None)
        if em and submitted_em_code and _level(em.code) > _level(submitted_em_code):
            events.append(FeedbackEvent(
                kind=FeedbackKind.CODING_EDUCATION, target=FeedbackTarget.PROVIDER,
                message=(f"This encounter was submitted as {submitted_em_code}, but the documentation "
                         f"already supports {em.code} ({em.rationale.split('.')[0]}). No additional work "
                         "is needed - the level reflects the visit you documented."),
                patient_benefit="Accurate coding of work already performed; no change to care.",
            ))

        return [e for e in events if self._assert_patient_benefit(e)]

    @staticmethod
    def _assert_patient_benefit(event: FeedbackEvent) -> bool:
        # Biller-targeted mechanics are allowed without a clinical benefit; every
        # provider/patient-facing nudge must articulate one.
        if event.target == FeedbackTarget.BILLER:
            return True
        return bool(event.patient_benefit.strip())
