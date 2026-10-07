# Durable header generation — Tier 2

Receiver-first, default-off async generation replaces request-lifetime inference only when
`billing_header_async_enabled` is enabled. Billing decisions/evidence, source hashes,
header edits, signoff and existing operation receipts are unchanged. PostgreSQL rung
028 adds durable source-version claims and operation bindings. A source can start one
pipeline only, including old synchronous callers. Polling never starts or retries work.

At most two jobs per practice and two children per process; no local waiting queue.
The parent supervises spawned children and terminates its own expired/shutdown jobs.
A crashed/expired attempt is unknown, never automatically rerun. Source/member/model
and Core authority are checked again before disclosure and atomic header/audit/success
persistence. No database transaction spans inference. Standalone identity represents
an accepted platform request, not an invented continuing JWT; Core additionally pins
original workload/provider-session/admission deadlines and renews only short current
source authorization within that original bound. Remote checks are point-in-time.
Async workers accept one extraction lane; secondary/TA4H/shadow engines fail before
model start because their threaded extraction does not carry the job disclosure guard.

Expand 028 with the locked runner; provision only its narrow EXECUTE capability to the
API/Core role. Disabled startup has no 028 dependency. Deploy CAB first, then the Core
async action migration/client and frontend; enable each flag only after compatibility
checks. No activation, live schema, vendor/model call or deployment is part of this PR.
Recovery disables new jobs, stops owned children and preserves claims, audits, headers
and unknown outcomes. No destructive down migration and no inferred retry permission.

Focused acceptance: duplicate/other-operation sync overlap, cross-process capacity,
restart/timeout unknown, successful atomic header/audit, changed source/member/policy,
Core original expiry and fresh authorization, and disabled compatibility. Independently
review exact final source before publication; record actual evidence and limitations.

Existing dedicated Core logins require the migration owner to grant only
`EXECUTE ON FUNCTION public.cosmaneura_header_generation(jsonb)` to that verified
Core runtime when activating this feature. Do not run the general API provisioner
on a Core login. New Core connection provisioning includes this grant only with
the async flag. Neither existing accounts nor deployments are changed by this work.
