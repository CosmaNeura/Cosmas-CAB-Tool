# SMS protocol and conversation foundation integration

## Outcome and risk

Tier 2: patient outreach protocol and suppression semantics. This bounded slice
ports the final SMS protocol, bounded HTTPS client, and local synthetic
conversation ledger from preserved feature commit
`8d0caca22aaed8001aaeec955c81c3f7ecc11bd4` onto reviewed header candidate
`a21310b8a30bfa046915de4709fa931de438000f`.

Seven ported files retain their final feature-source bytes; the transport test
module retains all original tests and adds one boundary check. That test verifies the current SMS-only default at both request construction
and the wire boundary. Existing historical voice protocol types remain internal
compatibility code; this release adds no call controls or enabled voice service.

The outcome is a complete, dormant conversation/protocol foundation: prepared
requests, validated provider acceptance, authenticated incoming replies and
statuses, exact retry identity, append-only local events, and contact-route STOP
suppression. The local SQLite store is for isolated synthetic rehearsals only.
It is not an operational database, shared-worker store or authorization source.

## Dependencies and rollout

1. This slice adds reusable modules and their existing focused tests only. It
   adds no imports to the running API, scheduler, settings or UI.
2. A later independently reviewed expand rung adds the final PostgreSQL ledger,
   dispatch lifecycle, cancellation and unknown-send controls, event review and
   unassigned inbox. Mounting the final runtime requires its entire canonical
   migration closure, not only the initial communications tables.
3. Later staff routes/UI and the isolated worker must enforce current case,
   practice, role, direct-contract, consent, quiet-hours, cadence and approved
   synthetic-recipient controls. A caller-supplied eligibility object here is
   not a request contract or permission to send.
4. Only a reviewed SMS activation after the user's Twilio setup and an approved
   recipient test may connect the worker. No account, number or provider
   configuration is changed by this PR.

No new dependency is installed. Existing httpx is imported lazily only by an
explicitly constructed transport. Its destination is the exact bound Twilio
account/resource, TLS remains verified, environment proxies and redirects are
disabled, one total deadline bounds the request, and no POST retry occurs.
An uncertain result remains unknown for staff investigation; acceptance never
claims delivery or patient contact.

## Data and invariants

The API and staging remain unchanged. No migration, production data, model call,
vendor call, text or telephone call is part of this integration. Local fixtures
contain reserved fictional numbers and synthetic credentials only.

Inbound callbacks require the canonical URL, signature, account and route/SID
binding. Duplicate or malformed fields fail closed. STOP suppresses the route;
START requests review and cannot create consent. Delivery/status replay cannot
regress state. An unknown send is never retried automatically. No inbound event
sends an automatic reply, alters an EHR, or changes a clinical source.

## Verification, review and recovery

Run the three ported protocol/transport/ledger test modules, including request
bounds, forged callbacks, wrong account/route, duplicate events, failed/unknown
submissions, exact replay, suppression and the SMS-only defaults. Run the
repository anti-inflation checks against this candidate. Use injected local
transports only, with outbound network denied during verification.

Freeze and push the exact commit for independent non-implementer review; bind
its findings and disposition to that SHA in the PR before merge. Keep this PR
within 2,000 changed lines and 50 files. It does not authorize deployment.
Recovery is to omit/revert this dormant integration from a future reviewed
build. There is no live schema/data/configuration to reverse. Preserve the
original feature worktree and all unrelated work.
