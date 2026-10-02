# Staff SMS API integration — Tier 2

Base: `73934cdd2f8c8c2afc53b9190f07e422fdedab45` (isolated SMS services).

## Outcome

Mount the existing complete authenticated SMS runtime: current-contact binding,
purpose-specific consent, separately authorized drafts, cancellation, unknown
send investigation, event review, and the unassigned text inbox. Worklist counts
contain no message/contact payload. Enabled Admin/BillingStaff receive the
explicit top-level durable-communications capability even with zero rows; it
remains absent for Coders and default-disabled training. Preserve exact retry, atomic audit and
current practice/member/case checks. Default enablement stays false; the API
never loads an outbound token or transport. Voice remains disabled by default.

Only required final SMS API/access-audit/provisioner hunks are ported from
`8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`. No Athena/provider/schema or demo
simulation route is introduced. Disabled communications return 404. The parent
dispatcher retains its corrected transaction-local database clock.

## Local acceptance

Retain the complete runtime, unassigned inbox and draft runtime test files.
Adapt only historical schema fixtures to the real locked sparse-chain runner
and a new owned disposable DB, instead of assuming an unrelated Athena table
or a fixed old test-database name. All acceptance definitions remain intact.
Copy exact pure DTO helper bytes for actual API JSON → Node contract checks;
the separate UI branch owns identical case/event helpers and components.

Run the actual API and worker provisioners in the new DB; verify authenticated
prepare/consent/authorize → fake worker → signed callback → another API replica,
revocation/unknown/cancellation/current-contact gates, audit rollback, and
unassigned sender review. No real transport, cloud/vendor call or credential
read. Retain failure evidence and run only affected corrections if necessary.
Required anti-upcode proof remains unchanged.

## Release and recovery

New provisioning capability is exercised only in the owned disposable DB.
Existing live roles/config/schema are untouched. A separately reviewed rollout
must reconcile current schema/grants and every sender/consent/activation gate;
this source integration supplies none of those approvals. All messaging latches
remain off. Readiness fails closed when required communication grants disappear.
Revert the unactivated application slice and leave immutable data/schema intact.
No destructive rollback or hosted/live-texting readiness claim.

Final pushed source/evidence lives under
`outputs/mcd-training-2026-10-02/sms-staff-api-integration/`. Independent agent
review after the final substantive push is required; it is not GitHub approval.
