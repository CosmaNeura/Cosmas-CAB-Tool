# MCD synthetic-training release wiring
Tier 2 — deployment, service identities and secret boundaries.

## Outcome
Make the already merged header, Athena and SMS runtime deployable through the
existing templates, and add a separate dormant Core workload app. Synthetic
training remains the first cohort; Session Prep stays outside CAB. No deployment,
credential access, source inference or messaging occurs in this change.

## Source and boundaries
- Base: merged CAB1c9688c4 (receiver026 included). Port the preserved8d0caca app/SMS templates and complete
  deployment tests. Narrow deployment channels to SMS under the current request.
- Core receiver dependency: PR84, merged1c9688c4; its tree equals the accepted
 26-rung union4e750812. No receiver code edits.
- Shared image digest; independent worker/callback/Core identities. API receives
  no Twilio token, worker DSN or Core policy. Core gets its new026-registered login,
  workload/model policies and signer; no browser-cookie or caller-header shortcut.
- Every new provisioning/ingress/inference/dispatch flag is false. Empty Core
  policy exposes disabled liveness only. Public workload ingress is explicit;
  private reachability across ACA environments must be established, not assumed.
- Owner ledger recorded001–009. Final package reconciles every exact001–026 hash
  and complete runtime grants. Do not reuse the obsolete011/013-only plan.

## Validation
Compile local Bicep and run complete imported deployment contracts plus focused
Core isolation/default/probe assertions and adjacent infrastructure tests.
Run required invariant/log checks once. Build one final combined Linux image from
the final merged source using cached dependencies; no vendor/model/hosted calls.
Retain exact source/image/migration hashes and any initial failures.

## Promotion and recovery
Review the nonsecret delta against fresh target observations; preserve existing
identity/auth/database settings. Training uses the fixed12-note owner seed and
named Entra/practice memberships. Signer secret reference and staff inputs are
operator-owned, never fabricated. Additional service activation is separate.
Restore a compatible digest or disable the affected runtime flags to stop new
work. Preserve additive schema/source/headers/audits and late SMS callbacks.
Conditional false does not delete resources or revoke previous grants.
