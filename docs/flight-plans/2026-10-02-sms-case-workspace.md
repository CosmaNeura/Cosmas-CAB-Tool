# SMS case workspace integration

Tier 2: staff outreach authorization and communication evidence display.

## Outcome

Stack on the reviewed CardChase workspace at 3f6d57d. Bring the six complete
case-conversation and event-review components/contracts/tests from preserved
8d0caca into the focused case workspace. The server must advertise an available
communications capability before its tab appears; default training/staging
responses keep their existing Summary and Activity tabs.

Staff can inspect current contact and purpose-specific permission, save a draft,
explicitly authorize it, cancel before dispatch, review replies, and record a
vendor-confirmed investigation of an uncertain outcome. Prepared, authorized,
accepted, sent and delivered remain distinct. An uncertain command retries the
same action identifier. Late responses cannot populate a different case.
SMS-only workspaces offer no new call or call-authorization controls. Historical
call evidence remains readable with cancellation of an unstarted operation.

## Boundaries

No API, schema, environment, credential, worker or vendor configuration changes.
These views consume the independently integrated final SMS backend contracts;
this PR does not activate or prove live texting. No direct practice-record link.
The clinical note/header pipeline and provider hosting remain unchanged.
The global unassigned-text inbox and board-wide automatic count polling belong
to the following workspace outcome; selected conversation and review counts
already refresh without replacing an unfinished staff draft.

## Verification and recovery

Retain complete source contract tests and the complete operational browser suite.
Use cached local dependencies and fixed synthetic HTTP fixtures to exercise the
actual built frontend, including explicit authorization, paused drafts, STOP,
exact retries, cancellation, unknown outcomes and case-switch response races.
No browser request may leave the local fixture origin. Verify desktop/tablet
layout and run the required billing invariant checks once. Record source hashes,
results and the final pushed independent review. Keep the PR below 2,000 changed
lines and 50 files. Recovery is a reviewed revert of these UI changes; no stored
records or schema need removal. Staging remains unchanged.

## Observed result

The production frontend build, 24 contract checks, 15 local browser scenarios,
10 guardrail checks and 4,000 invariant cases passed. Browser scenarios include
the complete preserved operational and event-review assertions, plus unavailable
capability coverage. Desktop/tablet screenshots confirm the responsive layout.
HTTP fixtures replace the backend only for this UI acceptance; shared storage,
worker permissions and live-provider acceptance are separate outcomes.
