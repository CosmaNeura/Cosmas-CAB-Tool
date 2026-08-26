# Flight plan: server-owned prepared billing packet

Status: **draft for Tier 2 approval**. This plan authorizes no implementation,
migration, activation, Athena access, EHR write, claim transmission, patient
outreach, deployment, or real-PHI use.

## Outcome and boundaries

- Problem and measurable outcome: the provider billing review tab builds a
  browser-assembled prepared preview (`preparedBillingBlock`) and the coder UI
  records feedback, but neither returns a server-owned, immutable billing
  artifact. The target is a versioned JSON packet whose
  source snapshot, human decisions, evidence references, completeness result,
  provenance, digest, and `prepared_not_transmitted` status are generated and
  durably bound by the server. Exact retries return the same packet; changed
  requests, stale encounters, incomplete documentation, unauthorized roles,
  and cross-practice access fail without a partial write.
- In scope: an immutable review snapshot, canonical packet contract, additive
  PostgreSQL structures, forced RLS, append-only persistence, atomic feedback +
  packet + access-audit completion, Coder/Admin prepare/read/download APIs,
  a dark server capability, and synthetic acceptance.
- Explicit non-goals: claim submission; clearinghouse, payer, Athena, or EHR
  writes; Athena OAuth/import/checkpoint changes; patient messaging or
  scheduling; provider-note changes; official CPT descriptor distribution;
  recording an external transmission; enabling real PHI; or connecting the
  separate Sovereign lab product.
- Affected roles and data: Coder and Admin may prepare and read a packet.
  Provider and BillingStaff are denied. The packet contains minimum-necessary
  billing codes, bounded evidence excerpts, rationale, and opaque references,
  so it is PHI-bearing whenever its source is PHI. Initial activation is fixed
  synthetic data only.

### Contract invariants

1. The browser never supplies an authoritative practice, tenant, reviewer,
   encounter version, source, engine result, disposition, evidence quote,
   completeness result, timestamp, status, or digest.
2. Every visible or final code has at least one server-verified evidence
   reference. A no-evidence candidate is omitted; only a count of suppressed
   candidates may be retained.
3. Every recommendation begins unreviewed and appears exactly once in the
   prepare request. Accept keeps the exact supported code and its original
   evidence. Every edit, rejection, override, and human addition requires a
   controlled reason, rationale, and server-verified evidence. A held or
   removed code cannot be restored as an accept action.
4. The server computes completeness from the stored encounter version.
   `prepared_not_transmitted` requires deterministic complete or not-applicable
   status. Today's browser-supplied override is not packet evidence; durable
   provider attestation is a separate Tier 2 slice.
5. The packet is append-only and always says `prepared_not_transmitted` with
   `transmission.performed=false` and no destination. No later state can imply
   that CAB transmitted it.
6. After authorization, capability/schema/store readiness, and RLS, a completed
   packet lookup and normalized request-hash comparison occur before current
   fixture, manifest, allowlist, or encounter checks. An exact retry returns the
   digest-verified stored packet byte-for-byte; changed content returns 409.
7. The clinical note and encounter are never modified by snapshot or packet
   creation.
8. Initial creation is not authorized by identity or a generic "synthetic"
   label. A first snapshot and first packet require PostgreSQL and an exact
   persisted row match to the immutable seed manifest bundled into the
   deployed image, including `synthetic_demo_release_candidate` source,
   `version=1`, null ingestion identities, every visit field, and the case and
   combined seed digests. Every other source or fixture store fails closed.
9. Initial packet inference is isolated from practice history. It injects an
   explicit disabled adaptive-preference provider that neither constructs nor
   queries a feedback store and records `adaptivePreferencesApplied=false`
   with no revision. Source-scoped learning is a separate future design.
10. Packet v1 has one decisioning inference path and records
    `multiEngineDecisioningApplied=false`. A non-influencing shadow may remain
    active, but its output is excluded from the packet. Active consensus or any
    secondary influence fails packet issuance until a versioned provenance and
    adjudication contract is separately approved.

## Risk and dependencies

- Tier and reason: **Tier 2**. This adds PHI-bearing schema, evidence snapshots,
  authorization-sensitive APIs, a durable billing artifact, and a multi-table
  transaction. It does not change billing adjudication, but it freezes its
  output into an auditable record.
- Billing/evidence decisions: the packet consumes only the deterministic rules
  path with adaptive preferences hard-disabled. It never adds or raises a code.
  Coverage may preserve the engine's ordering but may not add or suppress codes
  for payment reasons. Generic labels remain the default; official AMA
  descriptors are excluded unless the practice's license and distribution
  right are verified.
- Multi-engine boundary: current Language/TA4H shadow operation may remain
  observational because it cannot affect decisions. Packet v1 does not encode
  consensus provenance; enabling a secondary decisioning path makes snapshot
  issuance unavailable rather than silently collapsing multiple models into
  the primary model label.
- Required predecessor: merge the provider/coder demo slices that bind review
  context to the server-owned recommendation manifest and require affirmative
  coder decisions. Legacy `/api/feedback` remains backward-compatible but can
  never mint a packet.
- Cross-repo boundary: packet generation is internal to CAB and consumes an
  already normalized stored encounter. It imports no Athena, scheduler,
  messaging, clearinghouse, or Sovereign module. A future Cosmas provider
  widget is a separate receiver-first internal contract; core Cosmas owns its
  placement and encounter authorization, while Sovereign owns provider labs.
- Athena boundary: the Billing Import application identity is unresolved and
  must not be inferred from a display name such as `Cosmaneura CAB Pilot`;
  ownership is confirmed only by verifying its client ID, scopes, and
  separation from Sovereign (see the repo's Athena boundary docs).
  Billing Import and Sovereign Provider Labs keep distinct OAuth clients,
  secrets, token caches, identities, vault grants, and scopes. Background CAB
  billing reads require a separate architecture decision before activation;
  this packet plan neither needs nor authorizes them.
- Migration numbering: no future feature owns `010` in advance. Each migration
  PR rebases on current `main` and takes the next unused monotonic number. The
  packet and future normalized-coverage migrations must never receive the same
  number or merge out of order.

## Versioned API and packet contract

### Review snapshot

`POST /api/coder-review/snapshot` accepts only a schema version, UUID review ID,
and bounded encounter ID. Coder/Admin authorization and the active membership
come from the trusted server principal. An existing exact snapshot replay is
returned after stored-digest validation; a changed binding under the review ID
is 409. For first issuance, the server requires the PostgreSQL encounter/packet
stores and applies the independent packet fixture gate. One shared fixture-
manifest API, also consumed by the owner-only seeder, returns the pack ID,
expected exact persisted row, case digest, combined seed digest, and algorithm
version. The gate compares every expected row field and never reimplements seed
canonicalization with the packet JSON encoder. Source names, caller labels,
identity, and encounter-ID prefixes are never sufficient.

The server then runs the deterministic billing rules path with a stateless
disabled adaptive-preference provider. That path must not construct, query, or
load the practice feedback store. It applies deterministic completeness,
removes all no-evidence codes, and prepares
`cosmas.cab.review-snapshot.v1`. In the commit transaction it reselects the
same encounter UID `FOR SHARE`, reruns the exact persisted-row/manifest gate,
and holds that lock through snapshot insertion and access-audit completion.
This closes an owner seed/reset race between pre-inference validation and
snapshot persistence.

Snapshot insertion and its allowed access-audit completion commit in one
database transaction. The attempted audit reservation remains a prior durable
event. If membership, snapshot validation, insertion, or completion persistence
fails, no snapshot row is committed and no response may claim issuance.

The snapshot contains:

- server UUIDs for the snapshot and per-snapshot opaque practice and encounter
  references; database keys never become request authority;
- encounter version, source/provenance, canonical source snapshot digest,
  fixture pack ID, case digest, manifest version, and combined seed digest;
- engine, structured ruleset/configuration, inference-mode/model, fixed
  `adaptivePreferencesApplied=false`, and fixed
  `multiEngineDecisioningApplied=false`, without preference revision, shadow
  output, endpoint, or secret;
- server UUID recommendation/evidence IDs, kind, code, generic label,
  disposition, rationale, guardrail actions, primary flag, verified source,
  one or more exact ordered UTF-8 byte segments, bounded source slices, and
  per-segment digests;
- deterministic completeness rulepack/version and results; and
- `clinicalNoteModified=false`, degraded state, issue time, schema version, and
  canonical snapshot digest.

A degraded result is inspectable but not preparable. Exact snapshot replay by
the same actor/review/encounter returns the digest-verified stored snapshot
without requiring the old fixture to remain on the current allowlist; any
different binding under that review ID returns a generic 409. An unprepared
historical snapshot must pass the current fixture gate before it can mint a
packet.

### Prepare request

`POST /api/prepared-billing-packets/prepare` accepts only:

- schema version, review ID, review-snapshot ID and echoed snapshot digest;
- one explicit accept/edit/reject decision for every rendered recommendation,
  keyed by server recommendation ID;
- bounded manual additions with a client UUID, kind/code, controlled reason,
  rationale, and either a server evidence ID or source/UTF-8-byte span + text
  digest for server validation; and
- integer elapsed milliseconds.

Unknown, duplicate, missing, or cross-snapshot IDs fail. Client quote text is
never authoritative. The server validates a cited span against the immutable
stored encounter version, rejects duplicate final `(kind, code)` pairs, and
derives every identity, timestamp, provenance field, status, and digest.
Before any first prepare write, it also locks the same encounter UID and
revalidates the complete persisted row, snapshot fixture source,
pack/case/combined digests, shared manifest version, PostgreSQL binding, and
current deployment allowlist. A disabled or changed gate cannot mint a packet
from a previously issued snapshot.

The packet path adds a canonical evidence re-anchoring step after the existing
verifier. It maps each verified exact, normalized, or ellipsis-separated quote
to one or more exact, ordered, non-overlapping source byte segments while
retaining a mapping to the original source slice. A repeated or normalized
match that has more than one valid segment sequence is ambiguous and is
suppressed; an unanchorable or out-of-bounds quote fails closed. New human
evidence must cite an existing server evidence ID or an exact source byte
selection whose digest the server recomputes. Golden tests cover Unicode byte
offsets, normalized whitespace and punctuation, repeated phrases, ellipsis
segments, ambiguity, and impossible anchoring.

### Packet and retrieval

The immutable response contract is
`cosmas.cab.prepared-billing-packet.v1`:

- `packetId`, `packetVersion`, fixed `status=prepared_not_transmitted`, and
  server `preparedAt`;
- opaque practice/encounter references and source encounter version/digest;
- review/snapshot IDs and digest, engine/rules/config versions, inference mode,
  allowlisted model label, and `adaptivePreferencesApplied=false` with no
  adaptive-preference revision;
- original evidence-backed recommendations and dispositions, completeness
  snapshot, explicit human decisions, additions, reasons and evidence;
- ordered final/excluded code sets with engine-versus-human origin;
- reviewer opaque ID, review time, `clinicalNoteModified=false`, optional prior
  packet reference, and canonical packet digest; and
- `transmission={performed:false,destination:null}`.

`POST /api/prepared-billing-packets/read` and `/download` accept a packet UUID
in a bounded JSON body. Both require Coder/Admin and same-practice RLS. Download
returns canonical UTF-8 JSON with `Cache-Control: no-store`; its filename uses
only the opaque packet UUID. There is no submit, send, Athena, EHR, payer, or
clearinghouse route. Browser copy uses the returned server packet and stores
nothing in localStorage or analytics.

### Canonical digest and replay

Fixture-manifest v1 is a distinct shared contract used by both the owner-only
seeder and packet gate. It preserves the current seed algorithm exactly:
sorted compact JSON with `ensure_ascii=true`; case digest over
`{case_id,pack_id,source,visit}`; and combined seed digest over
`{seed_id,packs,cases}`. The shared API extends each prepared case with its pack
ID and exposes the manifest algorithm/version and expected persisted-row shape.
Neither caller may copy the algorithm or substitute packet canonical JSON.

Canonical JSON v1 is UTF-8, sorted object keys, compact separators,
`ensure_ascii=false`, `allow_nan=false`, no floats, and ordered arrays. It does
not normalize Unicode. Snapshot and packet digests are SHA-256 over the
canonical body excluding the corresponding digest field. Golden vectors pin
this rule across Python and browser tests.

Each idempotency request hash is SHA-256 over canonical JSON v1 for the entire
strictly parsed, schema- and domain-valid request. No request field is omitted;
`elapsedMilliseconds` is included and frozen by the client for exact retry.
A changed canonical request under the same review ID returns 409.

Every digest-bearing array has one domain order: rulesets use unique names and
lexical `(name, version)` order; recommendations use contiguous ordinal;
snapshot evidence uses first-reference order across recommendations and then
lexically ordered completeness rules; all evidence-reference arrays follow
that catalog; segments use ascending byte position; decisions and exclusions
follow recommendation order; manual additions use lexical client UUID order;
additional evidence follows its manual addition; final codes retain accepted
recommendation order followed by manual additions; and guardrail actions retain
deterministic pipeline order. Alternative or duplicate orderings fail closed.

Idempotency is `(practice_id, review_id)`. After authorization, feature/schema/
store readiness, and RLS, prepare first looks up a completed packet and compares
the normalized request hash. Exact retry verifies the stored payload digest and
returns its original ID, version, payload, digest, timestamp, and
`replayed=true`, even if the encounter, image manifest, or deployment allowlist
later changes. A changed normalized request under that review ID returns 409.
Only a first prepare applies the current fixture/encounter gate. A new review on
the same encounter may create the next version and reference the prior packet;
old packet bytes and status never change. Read/download requires authorization,
RLS, and stored digest integrity, not the current seed allowlist. Flag-off may
make all packet routes unavailable, but re-enabling a newer exact image cannot
orphan accepted history.

## Current deployed state

- Read-only verification on 2026-08-26 found GitHub `main` at `0861c9b`, with
  CI and CodeQL green and seven required status contexts (the `change-budget`
  check is a separate workflow, additionally required by repository
  administration). The local demo stack
  ending at `3426937` is not pushed or merged.
- Azure is still running revision `cosmaneura-pilot-api-i4akwqry--0000021` on
  immutable digest `sha256:95a8d1b5a3b0f3743e1e03ec5edbdef46390aa33fece0e3bd0179c2e8192f640`.
  Its candidate discovery tag does not independently prove source-to-manifest
  identity. No packet schema or route is deployed.
- Athena sync/import provisioning and execution are off. Patient messaging and
  scheduler are off; chase transport is `record_only`. Azure OpenAI is enabled
  only behind the reserved synthetic-source gate. These settings remain
  unchanged by this plan.
- The latest retained artifact is for older source `f490e65`; there is no
  retained artifact for current `main`. The repository has no configured
  GitHub deployment environment or deployment record.
- Reconciliation decision: **retain live temporarily**. Do not replace or
  mutate it during packet development. Reconcile digest, migration ledger,
  configuration, and deployed-only behavior before any later promotion.
- Previous data-recovery boundary: PostgreSQL has seven-day backup retention,
  with no HA, geo-redundant backup, automated restore, or approved RPO/RTO.
  That blocks real-PHI packet activation.

## Release control

- Add `COSMANEURA_PREPARED_PACKETS_ENABLED=false`, owned by the release owner.
  It is false in every existing environment. The server advertises a packet
  capability only when the flag is true and packet-store readiness, schema,
  role, and privilege checks pass.
- Add a separate
  `COSMANEURA_PREPARED_PACKET_ALLOWED_FIXTURE_DIGESTS` setting, empty by
  default. For the first activation it accepts exactly the approved combined
  seed digest, not a wildcard. Startup fails if the configured digest is
  malformed, unknown to the shared fixture-manifest API in the deployed image,
  duplicated, or combined with a non-PostgreSQL packet store. The setting is
  non-secret but remains a release-controlled allowlist; the feature flag
  cannot bypass it.
- Flag off means new routes return a fixed unavailable response, the provider
  billing review tab retains its reviewed prepared-preview behavior, and packet
  tables are not an
  ordinary readiness dependency. It never falls back from a failed packet call
  to a browser-generated success.
- Initial activation is an exact-image, exact-seed-digest synthetic-only cohort
  with Athena, messaging, scheduler, EHR writes, claim submission, and real PHI
  off. `synthetic`, `deidentified`, `manual`, `athena`, `explicit_import`,
  `athena_preview_synthetic`, missing/unknown provenance, ad-hoc in-memory
  fixtures, and same-prefix IDs are explicitly ineligible.
- The flag is behavior recovery, not schema rollback. Remove it only after the
  accepted observation window and a separate reviewed PR.

## Schema/data sequence

| Change | Rung | PR | Invariant | Recovery |
|---|---|---|---|---|
| Add immutable review-snapshot and packet tables, nullable legacy-feedback link, indexes, constraints, forced RLS, grants, and guards | Expand | One Tier 2 migration PR, number allocated after rebase | Existing binary ignores the structures; no route writes them | Leave dormant; never use destructive down migration as normal rollback |
| Add shared seeder/packet fixture-manifest API, no-preference packet orchestrator, canonical domain/store, digest-pinned gate, and atomic methods behind false flag | Dual-path, not dual-write | One Tier 2 code PR | Legacy feedback unchanged; packet inference never loads practice feedback; only exact bundled PostgreSQL rows enter the new path | Flag off and prior exact image; preserve rows |
| Expose versioned APIs/capability behind false flag | Dark read/write path | One Tier 2 API PR | Client identity and engine result never authoritative; denials/audit failures fail closed | Flag off; legacy routes remain compatible |
| Adopt server packet in the provider billing review tab and browser tests | Cohort cut | One Tier 1 UI PR | No browser packet success/fallback; prepared state comes from server receipt | Flag off restores the provider tab's prior prepared-preview behavior |
| Enable exact digest for fixed synthetic identities | Synthetic activation | Separately authorized release | Zero PHI/outbound effects and exact persona/evidence/replay acceptance | Disable flag, close ingress if needed, retain additive data |

The expand migration adds:

- `billing_review_snapshots`, keyed by practice and snapshot UUID, with exact
  encounter/reviewer foreign keys, opaque presentation refs, source version/
  digest, request/digest hashes, bounded versioned JSON and issue time; unique
  practice/review identity;
- nullable `coder_feedback.review_snapshot_id`; legacy feedback stays valid,
  while the packet transaction requires an exact practice/review/encounter/
  version relationship; and
- `prepared_billing_packets`, keyed by practice and packet UUID, with unique
  review, snapshot and feedback bindings, per-encounter packet version,
  optional same-encounter predecessor, fixed status, request/packet hashes,
  bounded canonical JSON and server preparation time.

Packet JSON is minimum necessary: bounded evidence excerpts, not full intake,
transcript, or note bodies. It is nevertheless PHI-bearing. All tables use
`ENABLE` + `FORCE ROW LEVEL SECURITY`, exact composite keys, RLS practice
policy, revoked PUBLIC access, and runtime `SELECT`/`INSERT` only. Runtime gets
no update/delete/truncate/trigger/schema authority. Enable-always guards make
rows immutable after grant drift. Snapshot issuance uses one transaction for
membership validation, same-UID `FOR SHARE` exact-row revalidation, snapshot
insertion, and access-audit completion. Packet preparation uses a separate
transaction for replay/conflict classification, active reviewer check, first-
write snapshot/encounter lock and exact-row/manifest/stale validation, feedback
+ decisions, packet version allocation, packet insert, and access-audit
completion. Each attempted audit reservation remains separate and durable; a
failed completion leaves no corresponding domain write.

Migration rehearsal uses disposable PostgreSQL 16 with the exact locked runner.
Prove old-binary compatibility, new-binary flag-off behavior, checksum/ledger,
forced-RLS cross-practice denials, runtime privilege limits, concurrency, and
leave-dormant recovery. No deployed migration is authorized by this plan.

## Rollout and promotion

Dependency-ordered PRs, each below 2,000 changed lines and 50 files:

1. Approved flight plan, followed by a contract-artifact-only Tier 1 slice with
   canonical schema examples, golden vectors, and dependency-free tests.
2. One dormant expand migration with DB and old-binary compatibility tests.
3. Shared seeder/packet fixture-manifest API, packet-specific no-preference
   orchestrator, canonical packet domain + PostgreSQL transaction behind the
   false flag, including the default-empty fixture gate, with no route.
4. Coder/Admin snapshot/prepare/read/download API and metadata-only audit behind
   the false flag; legacy feedback unchanged.
5. Frontend adoption and browser acceptance; flag false by default.
6. Optional durable provider completeness-attestation plan/implementation;
   until then, unpersisted overrides cannot produce a packet.
7. Separately authorized exact-main artifact, closed-ingress migration, fixed
   synthetic activation, acceptance, observation, and go/no-go record.

Hold each PR independently. Schema precedes writer, server contract precedes
browser sender, and CAB endpoint precedes any core-dashboard consumer. Every
code-changing PR receives independent non-implementer review after its final
substantive push; all seven required protected checks (plus the separate
`change-budget` check) must pass.

Synthetic canary acceptance requires:

- first prepare, lost-response retry, changed-payload conflict, concurrency,
  stale encounter/source/config, exact replay after encounter/image/allowlist
  rotation, read/download after rotation, and new-version predecessor behavior;
- fixture-gate denial before inference or persistence for an empty/unknown
  source, `synthetic`, `deidentified`, `manual`, `athena`, `explicit_import`,
  `athena_preview_synthetic`, wrong case/pack/combined digest, same-prefix
  impostor, changed visit field/version/ingestion identity, and every in-memory/
  file-store fixture; exact bundled PostgreSQL cases pass only when the
  deployment allowlist equals their combined seed digest; a same-external-ID/
  new-UID owner reset between precheck and snapshot commit fails;
- one shared fixture-manifest algorithm consumed by seeder and gate, with
  golden compatibility for the current base-pack, extension-pack, all twelve
  case, and combined seed `EXPECTED_*_DIGEST` values; packet canonical JSON is
  proven not to implement or alter the seed digest contract;
- mixed-practice feedback canaries and a query spy prove packet inference
  neither constructs nor reads the adaptive feedback store and always records
  `adaptivePreferencesApplied=false` without a revision;
- shadow-only secondary processing cannot change packet bytes, while any active
  consensus or secondary decisioning configuration blocks snapshot issuance;
- golden JSON/schema/digest vectors and corrupt stored-payload fail-closed;
- explicit decisions; unknown/duplicate IDs, changed accept code, missing
  reason/rationale/evidence (including rejection without evidence), invalid
  span/hash, held/removed restoration, duplicate final code, degraded
  inference, and completeness blocker tests;
- signed-out/forged-header checks; Provider/BillingStaff denial; Coder/Admin
  success; inactive membership between snapshot/prepare; cross-practice API/SQL
  isolation; missing practice context; forced RLS and immutable-grant checks;
- atomic rollback proving no snapshot without its completion and no packet/
  feedback without their completion on any failure;
- no packet body, code, quote, rationale, external encounter ID, raw URL,
  digest, credential, or token in logs/telemetry; and
- zero Athena/EHR/claim/payer/message/scheduler network or outbox effects.

Abort on any nonzero invariant, authorization, cross-practice, digest,
evidence, audit-atomicity, transmission, or PHI-log violation. Functional
failures keep the capability dark; no mock fallback is allowed.

## Security and recovery

- Open acceptance: real-PHI retention/legal hold/export/deletion policy;
  approved RPO/RTO and isolated restore drill; agreements/BAAs and service
  data handling; exact-image Entra persona/spoof/cross-practice acceptance;
  licensed CPT behavior; certified-coder validation; and cross-container
  PHI-safe log evidence.
- Data minimization: no full note bodies, contact/coverage details, Athena IDs,
  payer destinations, or unlicensed descriptor text. Responses are no-store
  and never enter URLs, analytics, caches, or localStorage.
- Recovery: disable the flag, close ingress for an authorization/data-boundary
  issue, and restore the last accepted immutable image. Leave additive tables
  and rows dormant. Never delete packet history or run a down migration as
  ordinary rollback. A digest or cross-practice anomaly is an incident.
- No automatic purge is introduced initially. Synthetic rows may be removed
  only by a separately reviewed reset. Real PHI remains blocked until retention
  and deletion are approved and implemented.
- “Record external transmission” is a separate append-only human-action record
  with its own flight plan. It never transmits or mutates this packet.

## Completion

- Required evidence: approved plan; exact contract/golden vectors; migration
  and privilege rehearsal; full Python/frontend/browser/anti-upcode/log-scanner
  suite; independent reviews; seven required protected checks plus the separate
  `change-budget` check; exact-main retained
  artifact whose bundled fixture manifest matches the configured allowlist;
  live reconciliation; exact-digest synthetic persona/source/replay/stale/RLS/
  log acceptance; and signed go/no-go with the capability synthetic-only.
- Complete means Coder/Admin can create, read, copy, and download the same
  digest-verified `prepared_not_transmitted` packet on exact retry, while every
  forbidden role, stale/changed request, evidence gap, completeness block,
  audit failure, and cross-practice attempt fails without partial state or
  external effect.
- Follow-up: remove the legacy browser preview only after stable cutover; remove
  the flag separately; consider legacy feedback deprecation separately; and
  update migration/deployment/access-control/data-retention records. No
  destructive cleanup is part of this plan.
