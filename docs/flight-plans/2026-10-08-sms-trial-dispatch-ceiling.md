# Durable practice-wide SMS trial ceiling — Tier 2

Base: `3c3650561148935fa58e6f10eb2f2bd41127e54e`.

The one-operation trial job currently limits only one batch. A second execution
or another case can start another send. Add an optional permanent practice-wide
limit using the existing append-only dispatch-start ledger and practice
transaction lock. No schema change, new database grant, vendor dependency,
call activation, billing adjudication or external authentication change.

`COSMANEURA_COMMUNICATIONS_TEST_MAX_DISPATCH_STARTS` defaults to `0` (existing
behavior). A positive integer `1`–`5` requires explicit SMS-only test mode. The
owner trial uses `1` on its API and outbound worker. Changing the positive limit
changes the approval-bearing policy hash. The current recipient allowlist,
consent, staff authorization, source/contact checks, direct-contract gate,
suppression, touch limits, quiet hours and deployment latches still apply.

At claim, count all permanent starts for this practice under the existing
PostgreSQL advisory transaction lock, then commit the reservation before any
provider request. Concurrent workers and different cases share the same ledger.
Canceled or blocked actions before a start consume nothing. A start is never
refunded by rejection, uncertain transport, restart or manual unknown resolution;
there is no reset API or automatic retry. Read/prepare/authorize expose exhaustion
without granting dispatch authority. A positive ceiling also rejects bodies
outside one GSM-7 segment, without changing or transliterating patient text.

The staff view shows the remaining allowance and explains that started sends
remain reserved even when delivery is unknown. The display is advisory; the
claim transaction enforces the ceiling. Default-zero policy hashes remain
compatible with current approvals. Callback handling remains active when
outbound latches are off and sends no automatic reply.

This is an outbound application limit, **not** an account financial cap. Inbound
texts, provider-generated STOP/HELP replies, carrier/provider fees and delayed
billing still require the separate owner-only five-message/USD1 ledger and
billing reconciliation. No live activation, consent, current image publication,
identity/grant, campaign registration or fee approval is inferred here.

Local acceptance: shared API/worker strict configuration and policy hashing;
GSM basic/extension/Unicode boundaries; validated allowance projection; actual
owned disposable PostgreSQL concurrent cross-case claims, unknown retention,
preclaim cancellation/blocked nonconsumption and non-GSM rejection. Keep the
existing anti-upcode proof and guardrail checks green. No vendor calls.

Release receiver/configuration first from a merged reviewed artifact; only then
apply the same positive test limit to the separately approved synthetic trial
API and worker. Preserve current staging and existing image10 history. Recover
by disabling the existing outbound activation/scheduler latches. Do not erase
starts, lower/reassign reservations, or disable the ceiling while test outreach
remains live. There is no migration rollback or shared-state cleanup.

Independent agent review after the final substantive push and the existing
normal protected checks remain required. Actual deployment and one manually
reviewed owner test execution are separate operational steps.
