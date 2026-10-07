# Communications contact activity — Tier 2

Owner request: complete operational beta communications. The contact log currently
omits durable Twilio dispatch attempts. Discovery F5/F9 and the user's explicit
last-contact/count/type requirement call for one useful staff activity view.

Scope: a read-only, practice-scoped projection of existing dispatch starts and
receipts alongside legacy/manual attempts. Count each start once, omit drafts and
pre-start cancellations, preserve unknown/rejected/delivery distinctions, and keep
history at 50 entries with full totals. Worklist reads batch case IDs. No phone,
message body, provider identifier or actor identifier enters the projection.

No schema, scheduler, consent, authorization, cadence, transport, activation,
patient data, or environment changes. Existing role checks and RLS remain in force.
No vendor calls. Synthetic fake dispatches only in an owned local test database.

Validation: focused projection/API and UI contract tests, restricted PostgreSQL
reads after fake SMS/voice dispatch and signed callbacks, bounded-history and
cross-practice checks, existing activity regressions and billing guardrails.
Parent independently reviews the local commit before publication.

Completed validation: 160 focused Python tests passed (56 activity, 12 restricted
PostgreSQL projection, 24 chase API, 41 security, 17 dashboard contract, 10 billing
guardrails); 7 frontend activity tests passed. `prove_no_upcode.py` passed its
2,000 guardrail and 2,000 consensus cases. No full-suite or build repetition.
The local database used a unique name on the task-owned container; no other
database was changed. Test fixture repairs retained all database triggers and
practice binding checks. No cloud or vendor request was made.

Recovery: revert this presentation commit. Durable outreach evidence remains
unchanged; no migration or data rollback exists or is needed.
