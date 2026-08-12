"""F9 insurance-data chase — end-to-end mock demo for the Aug 13 biller meeting.

Runs entirely against the mock athenaNet harness: zero PHI, zero credentials,
zero network. Every patient is synthetic; every message is simulated.

    python run_chase_demo.py

What it shows, in the billers' own terms:
  1. the system finds every patient whose insurance data is missing, expired,
     or inactive — before the visit, without anyone chasing;
  2. staff approve every message before it goes out (the team stays in
     control — the agent does the tracking, not the talking);
  3. a direct-contract patient is NEVER auto-chased — routed to human review
     (the hard gate Grace asked for);
  4. when a card lands through the CardCapture link, the item closes itself
     and eligibility re-checks ACTIVE;
  5. the metrics the team will judge it by, counted from day one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cosmaneura_billing.athena_client import MockAthenaNet
from cosmaneura_billing.chase import (
    ChaseQueue, QueueType, detect, on_card_verified, pretty_metrics,
    simulate_patient_submission, summarize,
)

NOW = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)
STAFF = "biller-demo"


def main() -> None:
    print("=" * 72)
    print("CosmaNeura — insurance-data chase (F9) · mock athenaNet · synthetic data")
    print("=" * 72)

    # 1 ── seed the practice ------------------------------------------------
    athena = MockAthenaNet(seed=20260813, now=NOW)
    athena.seed_world(n_patients=40)
    print(f"\n[1] Synthetic practice seeded: {len(athena.list_patients())} "
          "patients (incl. 2 direct-contract)")

    # 2 ── detect -----------------------------------------------------------
    items = detect(athena, now=NOW, manual_flags=("PT-1003",))
    queue = ChaseQueue(QueueType.CARD_CHASE)
    for it in items:
        queue.enqueue(it, now=NOW)

    review = [i for i in items if i.sequence_state == "human_review"]
    workable = [i for i in items if i.sequence_state == "open"]
    print(f"\n[2] Detector pass: {len(items)} patients need insurance attention")
    print(f"    {'chase item':34} {'trigger':22} {'priority':8} state")
    for i in items[:10]:
        print(f"    {i.chase_id:34} {i.trigger:22} {i.priority:8} {i.sequence_state}")
    if len(items) > 10:
        print(f"    … and {len(items) - 10} more")
    print(f"    → {len(review)} routed to HUMAN REVIEW by the direct-contract "
          "gate (never auto-chased)")

    # 3 ── staff-approved outreach -----------------------------------------
    to_chase = workable[:3]
    print(f"\n[3] Staff approves outreach for the top {len(to_chase)} items "
          f"(every send requires approval):")
    body = ""
    for it in to_chase:
        queue.approve(it.chase_id, staff_id=STAFF, now=NOW)
        body = queue.send(it.chase_id, template_id="card_request.v0",
                          now=NOW + timedelta(hours=1), sent_by=STAFF)
        print(f"    sent → {it.patient_ref} ({it.trigger}, {it.priority})")
    print("\n    sample message body (note: secure link only — never any "
          "insurance data):")
    print(f'    "{body}"')

    # 4 ── two patients submit; the loop closes itself ----------------------
    print("\n[4] Two patients submit cards via the CardCapture link; staff "
          "verify; Athena write is idempotent:")
    for it in to_chase[:2]:
        event = simulate_patient_submission(it.patient_ref,
                                            now=NOW + timedelta(hours=20))
        result = on_card_verified(queue, athena, event,
                                  now=NOW + timedelta(hours=20))
        print(f"    {it.patient_ref}: card verified → package "
              f"{queue.items[it.chase_id].resolution['package_id']} written → "
              f"eligibility {result['eligibility']} → item auto-closed")
        assert result["eligibility"] == "ACTIVE"
        assert it.chase_id in result["closed_items"]

    replay = on_card_verified(
        queue, athena,
        simulate_patient_submission(to_chase[0].patient_ref, now=NOW),
        now=NOW + timedelta(hours=21))
    assert replay["duplicate"] is True and replay["closed_items"] == []
    print("    replayed the first card event → duplicate=True, nothing "
          "double-written, nothing double-closed")

    # 5 ── metrics ----------------------------------------------------------
    print("\n[5] " + pretty_metrics(summarize(queue.items.values(),
                                              queue.staff_actions)))
    print("\nDemo complete — all rails held (approval-gated sends, "
          "direct-contract review bucket, idempotent writes).")


if __name__ == "__main__":
    main()
