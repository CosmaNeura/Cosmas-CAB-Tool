# Registered source workspaces

Tier 2; one provider/coder workspace outcome. No schema or clinical-rule changes.

The main queue accepted only synthetic fixtures, although the authenticated
receiver and header API already support registered completed provider notes and
admitted Athena notes. Standalone header links exposed editing but no coder
signoff. This change makes the existing full review workflow reachable for those
two server-owned sources.

## Contract

- The API projects the stored ingestion source for the two model-scoped sources.
  This metadata grants no access or inference permission.
- Queue entries require the known source and an explicit closed policy outcome.
  Header availability remains the release control for operational workspaces.
- Detail must match the selected source and pass the existing exact header-source
  and document-hash reader before any new coder model request.
- A denied policy permits saved-header reading/editing, but starts no coding
  request or new header generation. Signoff still requires the existing current
  authorized review, explicit decisions, and saved revision.
- Unknown imports remain excluded; existing synthetic and header-off behavior
  stays compatible. Original notes are never edited or certified as EHR signed.
- Existing authorization, model-data admission, feedback signatures, revision
  conflicts, and atomic header signoff remain server-enforced.

## Verification and recovery

Focused shared-reader and API projection checks cover source disagreement,
changed document bytes, denied permission, result downgrades, and synthetic
compatibility. One local restricted-Postgres provider acceptance drives the
actual browser from registered note to provider edit/save and coder signoff.
Its transport is fake; it establishes no vendor or hosted readiness.
Required invariant checks and a frontend build accompany exact source evidence.

Local outcome: 58 focused Node checks, three API projection checks, ten guardrail
checks and 4,000 supplied-fact invariant cases passed. The fresh database applied
all 26 canonical migrations and replayed without changes. The browser generated a
provider-source header, saved a Provider revision, and recorded a Coder signoff on
revision 1; the read/export tail confirmed durable attribution and unchanged
source with four fake model requests. Driver selector/contract mistakes are
retained in evidence; the successful mutations were not repeated. Final copy and
long-source-ID wrapping were checked against that same saved receipt.

Recovery: revert this UI/projection change; stored sources, edits, and signoffs
remain intact and existing standalone header links continue working. No
deployment, account setup, or clinical activation is included. Draft navigation
guards are supplied by separate PR88. This branch composes its callback with
source-policy handling on both provider and coder header mounts, including the
denied-generation saved-header path. Final integration checks passed all 17
existing frontend wiring contracts, 58 Node reader checks, the production build,
and nine navigation browser cases, including two denied-model exact-save cases.
The prior real-PG signoff evidence is retained; backend bytes are unchanged.
