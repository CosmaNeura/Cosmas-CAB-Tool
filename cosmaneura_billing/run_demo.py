"""Run the full pipeline on the demo encounter and print both end-user views.

    python run_demo.py            # offline, deterministic (MockLLM)
    ANTHROPIC_API_KEY=... python run_demo.py   # uses Claude for extraction
    ICD10_CCSR_CSV=/path/DXCCSR_v2023-1.csv python run_demo.py  # full code set
"""
from cosmaneura_billing import BillingOrchestrator
from cosmaneura_billing.schemas import Disposition
from demo_visit import SAMPLE_VISIT

LINE = "=" * 74


def _codes(items):
    out = []
    for s in items:
        ev = f"  ← \"{s.evidence[0].quote[:60]}…\"" if s.evidence else ""
        risk = f"  [denial: {s.denial_risk}]" if s.denial_risk not in ("unknown",) else ""
        out.append(f"    {s.code:<10} {s.label}{risk}{ev}")
    return "\n".join(out) if out else "    (none)"


def main():
    orch = BillingOrchestrator()
    print(f"\nKnowledge base: {orch.icd10.size} ICD-10 codes loaded "
          f"({'full AHRQ file' if orch.icd10.size > 1000 else 'offline subset'})")
    result = orch.process(SAMPLE_VISIT)

    # ---------------- PROVIDER VIEW ---------------- #
    print(f"\n{LINE}\n  PROVIDER VIEW  —  post-visit, encounter {result.provider_card.encounter_id}\n{LINE}")
    print(f"Summary: {result.provider_card.summary}\n")
    print("Captured on this encounter:")
    print(_codes(result.provider_card.captured_codes))
    print("\nDocumentation opportunities (no extra work — capture what you did):")
    for e in result.provider_card.documentation_opportunities:
        print(f"  • {e.message}")
    print("\nCare follow-ups (for the patient):")
    for e in result.provider_card.care_follow_ups:
        print(f"  • {e.message}")
    print("\nNot billed (transparency):")
    for s in result.provider_card.declined_codes:
        print(f"  • {s.code} {s.label} — {s.rationale}")

    # ---------------- BILLER VIEW ---------------- #
    w = result.biller_worksheet
    print(f"\n{LINE}\n  BILLER WORKSHEET  —  encounter {w.encounter_id}\n{LINE}")
    print(f"E/M code:          {w.em_code.code if w.em_code else '(none)'} "
          f"— {w.em_code.label if w.em_code else ''}")
    print(f"Primary diagnosis: {w.primary_diagnosis.code if w.primary_diagnosis else '(none)'} "
          f"— {w.primary_diagnosis.label if w.primary_diagnosis else ''}")
    print("Diagnoses (claim order):")
    for d in w.diagnoses:
        print(f"    {d.code:<10} {d.label}  [denial: {d.denial_risk}]")
    print(f"\nDenial-risk flags:")
    for f in w.denial_flags:
        print(f"  ! {f}")
    print(f"\nRequires provider sign-off before submit: {w.requires_provider_signoff}")
    print("\nAudit trail (every decision, in order):")
    for a in w.audit_trail:
        print(f"    {a}")

    # ---------------- FEEDBACK LOOP ---------------- #
    print(f"\n{LINE}\n  FEEDBACK LOOP  —  events emitted to the platform\n{LINE}")
    for e in result.feedback_events:
        print(f"  [{e.kind.value} → {e.target.value}] ({e.tone})")
        print(f"      {e.message}")
        print(f"      patient benefit: {e.patient_benefit}\n")

    print(LINE)
    print("Revenue integrity:", result.revenue_integrity_note)
    print(LINE + "\n")


if __name__ == "__main__":
    main()
