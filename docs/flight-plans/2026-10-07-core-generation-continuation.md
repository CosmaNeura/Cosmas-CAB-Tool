# Core generation continuation — Tier 2

Build on the reviewed durable async receiver, without changing billing decisions,
source hashes, claims, model policies or header mutations. This receiver-first
addition is off by default and applies only to an explicitly consented Core async
Generate. Ordinary async/synchronous requests and read-only status retain their
existing contracts. No additional CAB migration is needed; rung 028 remains exact.

Core records continuous, current same-session provider authorization. CAB accepts
its fresh authenticated callback only for the original full Generate digest and
pins the initial generation deadline. Every model request and atomic header write
rechecks current Core/source/member/model authority. Provider continuation cannot
extend that fixed deadline (at most 360 seconds and original source admission),
renew a finished/expired attempt, or replace the original workload JWT expiry.
Core's action/session evidence is distinct from the managed-identity app token.
The actual client gets that token from ManagedIdentityCredential; the 120-second
constant is an ordinary Core action grant, not a configured app-token lifetime.
No live token lifetime is asserted. An unusually short app token still stops work.

Deploy this compatible receiver before the Core continuation migration/client.
Enable `COSMANEURA_BILLING_HEADER_CONTINUATION_ENABLED` only with async enabled, then enable Core's separate
opt-in capability. Core owns its append-only continuous-authorization ledger and
rejects late or different-session renewal. Status requests never renew authority.
No credential, deployment, live migration, vendor call or PHI admission is included.
Recovery disables new consented jobs and preserves original claims/headers/audits;
already captured jobs retain their original bounded authority, not a new lease.

Focused proof uses actual restricted local PostgreSQL and a spawned full pipeline
with authored fake model responses: completion beyond the original short provider
expiry with continuous authority, disconnect/late continuation held, immutable hard
deadline and workload expiry, closed opt-in wire, and old async/sync compatibility.
Cross-service revocation is a fresh point-in-time check, never a distributed lock.
Independently review final source before publication; keep prior async evidence intact.
