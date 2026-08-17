"""Runnable insurance-chase demo — the artifact to walk the two staff through.

    python demo_chase.py

Nothing leaves the building: synthetic patients, an inert transport, no Athena
credentials, no PHI. The point is to turn the interview from "tell me about your
process" into "here is the process, tell me where it's wrong" — which is a far
better use of the time of the two people whose whole job this is.

Open questions this demo is designed to provoke answers to (all still open from
the Aug 12 synthesis §8, unanswered by the Aug 13 follow-up):

  Q3/Q10  cadence, tone, channel order, quiet hours, texting consent
  Q9      how many touches per card today, minutes per card, cards outstanding
  Q11     should direct-contract patients be chased at all?
  new     who approves the policy, and how often do they want to be asked?
"""
from __future__ import annotations

from cosmaneura_billing.chase import (
    ApprovalMode, Channel, ChasePolicy, CoverageSnapshot, SequenceRunner,
    Worklist, detect_all, summarize,
)
from cosmaneura_billing.chase.metrics import baseline_comparison
from cosmaneura_billing.direct_contract import DirectContractList

TODAY = "2026-08-17"
CLOCK = "2026-08-17T09:00:00+00:00"


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# A synthetic day's worth of the practice's actual failure modes.
PATIENTS = [
    CoverageSnapshot(
        patient_ref="PT-1001", appointment_at="2026-08-17",
        has_insurance_package=False, email="a@example.invalid",
        phone="+15550101", sms_consent=True, voice_consent=True,
        portal_enrolled=True),
    CoverageSnapshot(
        patient_ref="PT-1002", appointment_at="2026-08-18",
        has_insurance_package=True, card_image_on_file=True,
        eligibility_status="INACTIVE", email="b@example.invalid",
        phone="+15550102", voice_consent=True),
    CoverageSnapshot(
        patient_ref="PT-1003", appointment_at="2026-08-16",
        has_insurance_package=True, card_image_on_file=True,
        coverage_end_date="2026-07-31", email="c@example.invalid",
        portal_enrolled=True),
    CoverageSnapshot(
        patient_ref="PT-1004", appointment_at="2026-08-24",
        has_insurance_package=True, card_image_on_file=False,
        email="d@example.invalid"),
    # On the practice's direct-contract roster: must never be auto-sequenced.
    CoverageSnapshot(
        patient_ref="PT-DIRECT", appointment_at="2026-08-19",
        has_insurance_package=False, email="e@example.invalid",
        phone="+15550105", sms_consent=True),
    # No usable contact route at all.
    CoverageSnapshot(
        patient_ref="PT-1006", appointment_at="2026-08-20",
        has_insurance_package=False),
    # Clean — must not appear on the list.
    CoverageSnapshot(
        patient_ref="PT-1007", appointment_at="2026-08-21",
        has_insurance_package=True, card_image_on_file=True,
        eligibility_status="ACTIVE", email="g@example.invalid"),
]

SNAPS = {p.patient_ref: p for p in PATIENTS}
SENT: list[dict] = []


def transport(*, channel: Channel, patient_ref: str, subject: str, body: str) -> str:
    SENT.append({"channel": channel.value, "to": patient_ref,
                 "subject": subject, "body": body})
    return "sent"


def main() -> None:
    roster = DirectContractList(refs=["PT-DIRECT"], version="demo")
    clock = lambda: CLOCK  # noqa: E731

    rule("1. Detection — one pass over today's schedule and coverage state")
    items = detect_all(PATIENTS, today=TODAY, now=clock, roster=roster)
    worklist = Worklist()
    worklist.extend(items)
    print(f"{len(PATIENTS)} patients checked -> {len(items)} rows on the worklist")
    print("(PT-1007 is clean and does not appear — the list is only real work)\n")
    for item in worklist.rows():
        print(f"  [{item.priority:>3}] {item.patient_ref:<10} {item.bucket:<12} "
              f"{item.trigger.value:<22} {item.state.value}")

    rule("2. The master list, as the staff would filter it")
    counts = worklist.counts()
    print(f"open: {counts['open']}   needs a person: {counts['needs_person']}   "
          f"unassigned: {counts['unassigned']}")
    for bucket, n in counts["by_bucket"].items():
        print(f"  {bucket:<12} {n}")
    print("\nRows a person must decide on before anything is sent:")
    for item in worklist.rows(needs_person=True):
        print(f"  {item.patient_ref}: {item.review_reason}")

    rule("3. The policy — cadence, channels and approval (all configuration)")
    policy = ChasePolicy(
        practice_name="MyCatholicDoctor",
        upload_url="https://mycatholicdoctor.invalid/insurance",
        approval_mode=ApprovalMode.PER_POLICY,
    )
    print("BEFORE approval: nothing can be sent to anyone.")
    runner = SequenceRunner(policy=policy, transport=transport, now=clock)
    try:
        runner.advance(worklist.rows()[0], SNAPS[worklist.rows()[0].patient_ref])
    except Exception as e:                        # noqa: BLE001 - demonstrating the guard
        print(f"  blocked: {type(e).__name__}: {str(e)[:70]}...")

    policy.approve(by="grace.targonski", at=CLOCK)
    print(f"\nAFTER approval by {policy.approved_by}:")
    for step in policy.to_dict()["steps"]:
        print(f"  day +{step['day_offset']}  {step['channel']:<7} "
              f"performed by {step['performed_by']:<7} ({step['template_id']})")
    print(f"\n  approval mode: {policy.approval_mode.value}  "
          f"— approve once, the system works the list")
    print(f"  quiet hours:   {policy.quiet_hours[0]}:00 to {policy.quiet_hours[1]}:00")
    print("  ^ every line above is a config value, not code. This is what the")
    print("    interview should change.")

    rule("4. The system works the list — four passes, unattended")
    for pass_no in range(1, 5):
        print(f"\n-- pass {pass_no} --")
        for item in worklist.rows():
            snap = SNAPS[item.patient_ref]
            result = runner.advance(item, snap)
            action = result["action"]
            if action == "sent":
                print(f"  {item.patient_ref:<10} sent {result['channel']}")
            elif action == "staff_task":
                print(f"  {item.patient_ref:<10} CALL TASK for staff "
                      f"(the system does not place calls)")
            elif action == "awaiting_review":
                print(f"  {item.patient_ref:<10} waiting on a person")
            elif action in ("escalated", "unreachable"):
                print(f"  {item.patient_ref:<10} {action.upper()}: "
                      f"{item.resolution['reason'][:52]}")
            else:
                print(f"  {item.patient_ref:<10} {action}")

    rule("5. What actually went out — and what it did not contain")
    print(f"{len(SENT)} messages sent across "
          f"{len({m['to'] for m in SENT})} patients\n")
    example = SENT[0]
    print(f"  to {example['to']} via {example['channel']}")
    print(f"  subject: {example['subject']}")
    for line in example["body"].splitlines()[:4]:
        print(f"  | {line}")
    print("\n  No message contains a member id, policy number, group number or")
    print("  any other insurance value — enforced in code, tested, and the")
    print("  templates have no placeholder that could carry one.")

    rule("6. A person makes the call, and logs it into the same history")
    call_row = next((i for i in worklist.rows()
                     if any(a.channel is Channel.PHONE for a in i.attempts)), None)
    target = call_row or worklist.rows()[0]
    runner.log_staff_attempt(
        target, channel=Channel.PHONE, template_id="phone.call", actor="maggie",
        outcome="reached", minutes_spent=5.5,
        note="patient read the card over the phone; entered it manually")
    print(f"  {target.patient_ref}: maggie logged a 5.5 minute call")
    print("  -> the phone step is visible in the metrics instead of being")
    print("     invisible work, which is how minutes-per-card becomes real")

    rule("7. Closing — a submission is not a card on file")
    runner.mark_submitted(target, note="uploaded via the secure link")
    print(f"  {target.patient_ref} submitted -> state={target.state.value}, "
          f"still open={target.is_open}")
    runner.close_verified(target, verified_by="cindy", athena_write_id="ath-4471",
                          eligibility_status="ACTIVE")
    print(f"  cindy verified and wrote it -> state={target.state.value}, "
          f"still open={target.is_open}")
    print("  ^ only staff verification plus the write closes a row")

    rule("8. Metrics — instrumented from the first row")
    for key, value in summarize(worklist.items.values()).items():
        print(f"  {key:<26} {value}")

    rule("9. The baseline we still do not have")
    print(baseline_comparison(worklist.items.values())["note"])

    rule("Questions for the two of you")
    for question in (
        "How many touches does a card take today, and how many minutes?",
        "How many cards are outstanding right now?",
        "Can we text patients? Is there recorded consent, and where does it live?",
        "What order would you put the channels in, and how long between them?",
        "Should direct-contract patients be chased at all?",
        "Who approves the policy — and do you want to approve every message, "
        "or approve the policy once and see only the exceptions?",
        "What closes a row for you today, and who is allowed to close it?",
    ):
        print(f"  - {question}")
    print()


if __name__ == "__main__":
    main()
