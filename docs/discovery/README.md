# Customer discovery corpus

Six conversations, July 21 – August 13 2026. **Evidence, not spec.** Nothing here
overrides the hard constraints in `/CLAUDE.md`.

Read `00-requirements-synthesis.md` first — it consolidates all six into
build-ready requirements with a build/hold split.

## Chronology

| # | Date | Person | Role | File |
|---|------|--------|------|------|
| 1 | 2026-07-21 | Scott Weeman | Therapist, Fiat Health | `2026-07-21-fiat-provider-scott-weeman.md` |
| 2 | 2026-07-24 | Maura Buete | MCD family physician | `2026-07-24-mcd-provider-maura-buete.md` |
| 3 | 2026-08-06 | Grace Targonski, Johanna Adriano, Edgar Morgan | MCD billing team | *synthesis in Drive; Fathom recap summarized in §3 of the requirements synthesis* |
| 4 | 2026-08-06 | Christa Thornberry | MCD provider | *synthesis in Drive* |
| 5 | 2026-08-10 | Sherin George | MCD provider (RRM/fertility) | *synthesis in Drive* |
| 6 | 2026-08-13 | Grace Targonski, Johanna Adriano | MCD biller follow-up | `2026-08-13-mcd-biller-followup.md` |

Conversations 3–5 have full syntheses in the owner's Drive that have not yet been
committed here. Their requirements are captured in `00-requirements-synthesis.md`
§3 and §7 (traceability index), which is sufficient for build decisions; commit
the full syntheses when convenient so the quote anchors resolve locally.

## The design partner

**MyCatholicDoctor (MCD)** — ~300 providers, telehealth-first, Catholic. Stack:
Athena (EHR) → clearinghouse (EDI) → payer, with Acuity for scheduling. Migrated
from Tebra. Leadership: Kathleen Berchelmann MD and Greg Berchelmann
(cofounders). Billing: Grace Targonski (Director of Billing, CPC).

Live denial pressure is **commercial** (Optum/UHC), not Medicare — which is why
the engine's NGS-jurisdiction coverage data is necessary but not sufficient, and
why documentation completeness (F2) attacks the real denials rather than payer
rule adjudication.

## Standing constraints these conversations established

- **Net coder minutes must go down.** Grace: *"my gut is it's going to create
  more time for us… we have to review what you're sending in and the modifiers."*
  Any feature that adds coder review time has failed regardless of accuracy.
- **The clinical note is never altered.** Billing output is a separate view over
  the same note. Sherin: *"clinical tool first."*
- **Suggestions must be criteria-met.** The conservatism promise was made to
  customers on record (Maura 7/24, Sherin 8/10) and is only true because the
  deterministic pipeline enforces it.
- **Short outputs.** Scott: ChatGPT's output was *"actually a little bit too
  much."*
- **Org pays, not the provider.** $59/user/month anchored with MCD leadership
  since ≤ 7/21; three end users quoted; one verbal yes (waived for evangelism);
  zero payments as of 8/13.
