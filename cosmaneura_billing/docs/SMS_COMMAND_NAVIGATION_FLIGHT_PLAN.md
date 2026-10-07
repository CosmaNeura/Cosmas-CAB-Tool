# Preserve pending communications commands

Risk: Tier2, patient-outreach UI recovery. Existing consent, authorization,
direct-contract, channel and server idempotency controls are unchanged.

An unresolved communications mutation previously kept its exact retry body
inside the selected case component, while another case/filter/workspace could
unmount it. Keep navigation locked from mutation start through an uncertain
outcome until the existing same-action retry confirms its result. Read-only
history loads and staff event-review navigation preserve their existing behavior.
Do not store patient messages or commands in browser persistent storage.

Deployment: merge the reviewed patch, then adopt it through a separately
authorized immutable CAB image rollout. No staging changes from this PR.
Recovery: use the previous reviewed image; unresolved server operations still
require investigation and exact idempotent recovery, never a fresh send.

Validation: built-browser synthetic HTTP fixtures cover one uncertain prepare,
case/filter/section/workspace locks and exact command identity, then navigation
release after confirmation. Existing stale-read and staff-review navigation
tests remain applicable. No vendor requests, real contacts or cloud operations.
