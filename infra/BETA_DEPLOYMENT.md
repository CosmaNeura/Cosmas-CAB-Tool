# Operational beta deployment configuration

`app.bicep` now includes the editable header, optional staff-reviewed Athena
Preview reads and the separate SMS addon in `communications.bicep`.
These are prepared deployment definitions. All new feature, scheduler, outreach
and public-callback controls default off. No account or sender is created.
The outbound channel default is SMS only. The current change adds source-only
parameters; no Azure configuration has been applied.

Use the retained local package and restricted-role rehearsal evidence for the exact
merged source; the deployment templates do not run that rehearsal themselves.
Before any later Azure change, reconcile actual image/configuration/schema and
deployed-only behavior, select the approved merged commit/image digest, and
review the parameter difference. The normal runner expands through migrations
001–026: unassigned SMS requires 018, provider handoff requires 019, its
empty source admission registry requires 020, and Athena identity bindings
require 021; configured Athena review requires 022, operational read/storage
requires 023, optional provider service dates require 024, and provider model
source verification requires 025; the dedicated Core workload receiver requires
026. Retain
the exact migration hashes, restricted-role grants and acceptance evidence.
Nothing here authorizes patient data, live outreach or cloud promotion.

For operational Athena and registered provider coding, the API-only secure `modelDataPolicyJson`
parameter carries the separately reviewed policy described in
[provider model policy](../cosmaneura_billing/docs/PROVIDER_MODEL_SOURCE_POLICY.md). It defaults to empty and adds
no source permission by itself. Provider model-source verification additionally
requires025 and the API EXECUTE grant; v2 can authorize both source kinds in one
deployment while preserving v1 Athena policy/proof bytes. External Azure inference requires the synthetic-only
restriction or this configured policy; the API validates the policy and actual
runtime/source/service before a disclosure. Policy presence also rejects
ad-hoc and unapproved source inputs, even after expiry. Keep TA4H and shadow
extraction disabled for approved source coding. Stop every prior process
when revoking a policy; replacing traffic alone does not revoke in-flight work.

## Topology and credential boundaries

All services use the same immutable image. The legacy email job stays separate
and must be disabled when `enableCommunications=true`.

| Service | Identity and secret material | Behavior |
|---|---|---|
| Staff API | Existing API identity: API DB URL, Entra credential, chase gate key, shared review signer, optional CAB Athena credential and approved roster | Membership/role protected; prepares and authorizes outreach without a Twilio transport |
| Communications job | New SMS worker identity: existing practice-bound worker DB URL, Twilio account AuthToken, chase gate key, optional approved roster | No ingress; manual by default; batch of 25; permanent claim before I/O; no platform retry |
| Callback app | Separate callback identity: existing worker DB URL and Twilio account AuthToken only | Private ingress by default; signed forms; no outbound adapter; all dispatch latches false |
| Migration job | Existing migration identity and provisioning passwords | Locked migrations and restricted role grants; no Twilio credential |

This beta retains the reviewed shared **database role** for worker and callback;
it does not claim a narrower callback DB role. Their Azure identities are distinct
with secret-specific Key Vault grants. The account AuthToken is shared because
REST Basic authentication and webhook HMAC use it in the current adapter. It is
never granted to the API. Do not substitute vault-wide reader access.

## Operator-owned prerequisites

Credentials remain dedicated secret-name references. The optional tester list
below is a secure-value parameter, not a credential or a consent record. Use
the approved secret input process; keep nonempty values out of source, parameter
files, shell arguments/history, outputs and logs.

- `reviewContextSigningKeySecretName` must already contain a stable shared
  32–256 printable ASCII signing secret. **This is required even when the
  header flag is off**, because PostgreSQL coder review needs server-secret
  tokens. Every API replica uses the same value. Coordinate rotation through
  a revision change; older contexts become invalid and require refresh.
- Before `provisionCommunications=true`, supply the actual approved
  `twilioAccountSid`, E.164 `twilioFromNumber`, and existing
  `twilioAuthTokenSecretName`. Account ownership, number capability,
  registration, product-specific agreements/BAA and consent policy are external
  prerequisites. Provisioning does not satisfy them.
- `communicationsEnabledChannels` defaults to `sms` and is supplied identically
  to the API, worker and callback as `COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS`.
  The deployment contract accepts only `sms`; calls remain outside this rollout.
  Empty, duplicate, unknown or noncanonical values are rejected.
- `communicationsTestMode=false` and secure `communicationsTestRecipients=''`
  preserve the existing defaults. For an approved synthetic SMS trial, explicitly
  enable test mode and provide the comma-separated E.164 numbers of consenting
  testers. A list without test mode and test mode with non-SMS channels are
  rejected. Existing runtime validation rejects malformed numbers, duplicates
  and more than twenty recipients without echoing them. Empty test scope is
  valid configuration but blocks every outbound test send.
  The protected parameter propagates to `communications.bicep` as `secureString`.
  Only the API and outbound worker receive the actual list, through a Container
  Apps secret and `secretRef`; empty input creates no empty secret or dangling
  reference. No new Key Vault access is added. The signature-only callback shares
  test mode but has an explicitly empty list and no recipient secret: incoming
  replies/STOP events must remain processable outside the outgoing allowlist.
  These controls do not record consent, enable dispatch or approve real data.
- `twilioMessagingServiceSid` optionally supplies an `MG` SID followed by exactly
  32 hexadecimal characters to all three services. Empty uses no Messaging
  Service. The SMS request retains the exact `twilioFromNumber`, so this does
  not enable sender-pool selection. A configured service must contain that
  approved sender before any later live acceptance. Changing channels or service
  identity requires a fresh policy approval and a new operation authorization;
  it does not silently update already-authorized operations. Roll API and worker
  revisions together because each running process retains its startup settings.
- `directContractRosterSecretName` and `directContractRosterSecretVersion`
  optionally mount an approved JSON reference list. API and worker read the
  **same immutable Key Vault version**, at
  `/var/lib/cosmaneura/roster/direct_contract_patients.json`. The dedicated
  read-only mount preserves application cache and other storage. It is a small
  beta file subject to the Key Vault secret-size limit. Empty parameters retain
  the existing file path; a missing roster blocks outreach. Never use an empty
  roster to conceal unavailable practice data.
  The beta secret mount requires the legacy email scheduler/messaging disabled;
  it does not change that older worker's file-provisioning contract.
- `athenaReviewMode=preview` requires its dedicated `athenaReviewClientId` and
  `athenaReviewClientSecretName`. It is separate from legacy sync/import inputs.
  The fixed runtime cohort/read contract stays authoritative. Retained live Preview reads establish only the recorded synthetic requests;
  they do not establish completed-note availability for every encounter or
  authorize other source cohorts. Azure has no `mock` option.
- `athenaReviewMode=configured` uses the registered case patient mapping and
  additionally requires `athenaReviewCredentialEnvironment=preview|production`.
  Use the dedicated credential matching that environment. Configured Preview
  retains its synthetic cohort. Operational reads additionally require explicit
  `athenaDeploymentId` and `athenaStorageId` matching the operator-approved
  admission for the exact case mapping. Empty IDs keep these reads unavailable.
  These identifiers are configuration references, not identity attestations;
  registering them or supplying credentials does not grant permission. Approval
  to read/store an Athena note does not enable model generation or outreach.

## Disabled rollout and later activation

1. After source/image and migration acceptance, supply the mandatory signer
   reference and leave features off. Enable `enableBillingHeader` separately
   after source-linked edits and current-revision coder signoff acceptance.
   `enableProviderHandoff=false` is independent and requires the header flag
   when enabled, plus migration 019 and existing membership/role checks. It
   exposes the reviewed receiver; it does not change the runtime's rejection of
   arbitrary operational notes or grant host-platform identity/data admission.
2. Once the vendor prerequisites are satisfied, set
   `provisionCommunications=true` with sender/secret metadata. Keep
   `enableCommunications=false`, scheduler/messaging flags false, and
   `communicationsExternalIngress=false`. This prepares isolated services.
3. `communicationsCallbackOrigin` outputs the exact generated HTTPS origin.
   Use these POST paths on that origin:

   | Setting | Path |
   |---|---|
   | Number inbound SMS webhook | `/callbacks/twilio/sms-inbound` |
   | Per-message delivery status | `/callbacks/twilio/sms-status` |

   Outbound delivery callbacks are set by the adapter. Do not rewrite the
   signed origin, bypass staff authentication, log bodies, or enable recordings.
   A custom hostname needs separately reviewed certificate/domain binding;
   the generated hostname requires no additional source code.
4. Open callback ingress only for approved signed synthetic acceptance. Keep
   sends off. Verify signature rejection, commit-before-acknowledgment,
   duplicates, STOP/START, revoked grants and body/time limits. Historical signed
   call callbacks remain reviewable when calls are disabled; new scripted-call
   acceptance requires the separate future channel decision.
5. Set `enableCommunications=true` for the durable staff workspace. Actual
   sends require the separately reviewed change setting
   `enableCommunicationsScheduler`, `enableCommunicationsMessaging` and
   `activateCommunicationsMessaging` together, with approved public callbacks.
   Legacy `enableChaseScheduler`, `enablePatientMessaging` and
   `activatePatientMessaging` stay false. Runtime still checks current case,
   contact, purpose consent, direct-contract clearance, policy/staff approval,
   quiet hours, touch caps and unresolved indeterminate sends.

## Recovery and acceptance evidence

Disable both communications messaging flags and its scheduler to stop new
submissions. Retain `provisionCommunications=true` so redeployment updates the
existing job. Keep callbacks available for opt-outs and late events during
investigation; closing ingress requires a deliberate plan for those events.
Conditional provisioning false does not revoke previously deployed resources or
grants under incremental deployment.

Changing the test list changes the approval-bearing dispatch policy. Stop new
sends with the existing messaging/scheduler latches, coordinate API and worker
revisions, and obtain fresh policy/operation approvals before resuming. Updating
a Container Apps secret alone does not restart existing processes; verify both
services loaded the intended scope without printing its values. Never disable
test mode as a way to stop a trial: that removes a restriction. Empty test scope
blocks sends, but stopping the existing dispatch latches is the primary control.
Keep callback processing available for late events. Default-false source values
are not evidence of current live state or permission to overwrite live drift.

Each service instance is bound to one account, AuthToken and from-number. During
an account or sender transition, preserve the old **signature-only callback**,
its original HTTPS URL and credential/configuration until late STOP and status
events have been handled. Plan a separately named parallel receiver deployment;
the single-instance addon does not automatically retain former credentials or
URLs. Keep the old outbound worker disabled. Do not simply replace the token
in place and claim coverage for old signatures. Retained recipient-level
suppression continues across sender/account changes, but it cannot capture an
opt-out that an unavailable old receiver never accepted. Retire that receiver
only after reviewing outstanding operations and the provider retry window.

Cancel unstarted drafts individually. An unknown result requires investigation
and explicit resolution; never resend or create a replacement merely because a
response was lost. Preserve ledger/audit/consent/suppression/source history, and
check additive-schema compatibility before restoring an accepted image.

Retain source commit, image manifest, migration hashes, parameter changes, role
grants and synthetic acceptance counts. Inspect callback/job platform and console
streams with source/credential canaries. The existing deployment-count scanner
does not automatically certify these new resource streams; retain explicit
operator evidence instead of reusing older email-worker acceptance. Real delivery,
payer eligibility and EHR writes are not established by these local checks.

Local `tests/test_beta_deployment.py` checks the compiled app/addon and security
boundaries. Compilation makes no Azure request or deployment.

## MCD synthetic training and the separate Core receiver

For the first MCD session, keep the existing staff API authentication, tenant,
practice, managed identity and synthetic-only inference restriction. Review
`enableBillingHeader=true`, the shared signer reference, and
`enableAzureLanguageShadow=false` against the actual live configuration.
Leave Athena, provider intake and SMS activation off until their own inputs are
ready. `provisionCommunications` and `provisionCoreBridge` are not prerequisites
for standalone MCD training. These defaults are not a snapshot of Azure.

Run the current image's locked owner command
`python -m cosmaneura_billing.migrations` through the existing migration job,
with its owner DSN and runtime provisioning passwords supplied by secret
references. It applies the complete001–026 set and then reconciles ordinary API,
worker and optional importer grants. Compare the entire ledger before and after;
the October2 owner read established only001–009. Do not run a hand-picked
011/013 subset or silently replace a checksum. Keep the additive tables on rollback.

The owner then runs `python -m cosmaneura_billing.seed_mcd_training` with the
same practice/tenant binding. This is the fixed twelve-case September30 pack,
including fertility cases10–12; it is not the older `SYN-RC-*` pack.
[The seed runbook](../cosmaneura_billing/docs/MCD_TRAINING_SEED.md) defines the
exact digest, atomic conflict behavior and replay counts. Assign named MCD
users active same-practice `Coder` memberships and the matching Entra role;
`Coder` already permits the header and CardChase views. Use `BillingStaff`
for staff who only operate CardChase, and grant `Admin` only to the intended
practice administrator. Seeding does not enroll users or grant roles.

The optional `core-bridge.bicep` is a separate workload application running
`cosmaneura_billing.core_bridge:app`. It uses the same image digest but a new
identity and a new dedicated database login. It receives no Twilio, Athena,
owner or ordinary API credential and does not use browser EasyAuth headers.
The staff API keeps its existing login. Core's same-origin provider button and
backhaul endpoint belong to the Core repository, outside this template.

Core activation has two phases:

1. Prepare the dedicated database secret name and shared signer secret reference,
   then set `provisionCoreBridge=true` with a proposed new
   `coreBridgeDatabaseRole`. Keep `coreBridgePolicyJson=''`,
   `coreBridgeModelDataPolicyJson=''`, `enableCoreBridgeInference=false` and
   `coreBridgeExternalIngress=false`. Empty policy exposes disabled
   `/api/live` only; it does not read the database or accept intake.
   The deployment outputs `coreBridgeManagedIdentityClientId` and
   `coreBridgeFqdn` for the next phase. The referenced Key Vault secrets must
   exist even while the bridge is dormant.
2. Use the returned identity to configure the exact Core backhaul app-role grant
   and audience. Prepare the closed `cab-core-bridge-policy.v1` with inbound
   tenant/audience/caller/role, fixed `coreOrigin`, `coreTokenScope` and this
   `managedIdentityClientId`. Owner-only
   `provider_admission_cli register-core-connection /private/.../connection.json`
   creates the dedicated login and narrow026 grants; it refuses an existing
   API login. Register intended Core subjects with `register-core-actor`
   using separately reviewed private manifests. This projection does not
   convert Clerk IDs into Entra identities. Supply the resulting dedicated
   DSN by its secret reference. Configure the separate model policy and exact
   `coreBridgeDeploymentId` / `coreBridgeStorageId` only for approved
   synthetic/de-identified source/model scope. Then configure the workload
   policy; active readiness becomes `/api/ready`.

Private ingress is the default, not proof of reachability from Core. If the apps
are in different ACA environments, establish and verify the network route or
explicitly select `coreBridgeExternalIngress=true` with the nonempty exact
workload policy. Public ingress still requires the dedicated app-only token;
it does not mount the browser API. No template creates those app registrations,
role assignments in Entra or Core-side environment settings.

Set `enableCoreBridgeInference=true` only with the exact model policy and
service references. This condition grants the dedicated identity the Azure
OpenAI User role on the configured account; it leaves the staff API's model
policy and identity unchanged. Stop or retire all previous bridge revisions
when revoking model authority. Existing source/header/audit history remains.
See [provider model source policy](../cosmaneura_billing/docs/PROVIDER_MODEL_SOURCE_POLICY.md)
and [the receiver contract](../cosmaneura_billing/docs/PROVIDER_COMPLETED_NOTE_RECEIVER.md).
