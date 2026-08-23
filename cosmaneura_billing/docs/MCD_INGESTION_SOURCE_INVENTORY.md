# MCD insurance-source inventory and onboarding contract

Status: inventory structure ready; MCD system names, owners, contracts, and
sample exports still require operator confirmation. Unknowns are deliberately
not guessed.

Card Chase cannot assume Athena is the only source of insurance information.
Every MCD source will enter through the same source-agnostic import/checkpoint
layer, but each source retains its own authorization, feed identity, cursor,
idempotency receipts, mapping version, and kill switch.

Lab results are intentionally absent from this inventory. The provider lab
dashboard/chatbot remains owned by Cosmas Sovereign and uses a separate Athena
OAuth application and FHIR-only scope set; see
[`ATHENA_SYSTEM_BOUNDARIES.md`](ATHENA_SYSTEM_BOUNDARIES.md).

## Current inventory

| Source ID | System of record | Data supplied | Transport | Owner | Status |
|---|---|---|---|---|---|
| `athena_preview` | athenahealth Preview `195900` | Synthetic encounter, coverage, claim, and outcome reads after endpoint approval | OAuth 2.0, no-ingress job | Cosmas billing | Contract pending; disabled |
| `mcd_insurance_source_01` | To be named by MCD | Insurance/member identifiers and coverage evidence | Unknown | MCD | Discovery required |
| `mcd_insurance_source_02` | To be named by MCD | Insurance/member identifiers and coverage evidence | Unknown | MCD | Discovery required |
| `manual_card_chase` | Staff-reviewed patient response | Minimum necessary card/identifier update | Manual staff workflow | MCD billing staff | No automated outreach or ingestion enabled |

The placeholder source IDs reserve no implementation assumption. Add or remove
rows when MCD supplies the real inventory.

## Required facts for every source

Do not build an adapter until all fields below are answered and a sanitized,
synthetic sample is retained for contract tests.

- Product/vendor and MCD business owner.
- Whether it is authoritative, advisory, or a transport copy for each field.
- Environment and practice/tenant binding.
- Authentication method, credential owner, rotation period, and revocation
  process.
- Transport type: API, webhook, SFTP/file, queue, clearinghouse export, or
  staff upload.
- Exact read operations or file schema, version, pagination/cursor behavior,
  update/correction semantics, and documented rate limits.
- Stable source record ID and source version/change token.
- Fields available: payer, plan, member/subscriber ID, group, relationship,
  effective/termination dates, eligibility status, card images, and provenance
  timestamps.
- Whether records can contain real PHI, retention requirements, and BAA/data
  processing terms.
- Backfill window, normal cadence, outage behavior, replay window, and source
  reconciliation contact.
- Explicit write capabilities. They remain disabled and out of scope for this
  pilot even if the source credential technically permits them.

## Shared ingestion contract

Each adapter emits normalized records together with this envelope:

```text
target_practice_id     server-owned; never selected by payload/request data
source_system          stable inventory ID
feed                   stable logical feed within that source
source_record_id       opaque source identity; never logged
source_version         immutable version/change identity
payload_sha256         canonical normalized payload digest
checkpoint             opaque, encrypted-at-rest continuation state
mapping_version        reviewed adapter/normalizer version
```

One database transaction must insert or update normalized records, record the
idempotency receipts, and advance that feed's checkpoint. An exact replay does
nothing. A conflicting payload under the same source/version fails closed. A
failed page advances nothing. Operational output is limited to fixed codes and
counts—never patient, member, claim, source-record, cursor, or payload values.

## Reconciliation rules

- Never merge patients or policies using name/date-of-birth heuristics alone.
- Never let arrival order choose the winning insurance identifier.
- Configure field-level source precedence only after MCD names the system of
  record and approves the rule.
- Conflicting active coverage, member IDs, or effective dates create a human
  Card Chase review item with provenance; they are not silently overwritten.
- Corrections preserve prior source versions and the staff decision trail.
- Card images and unstructured attachments stay quarantined until a separate
  storage, malware-scanning, access, and retention design is approved.
- Import success never sends a call, SMS, email, or source-system write. Patient
  contact remains an explicit staff action in the pilot.
- A human verification may retain an optional, opaque `source_write_id` receipt.
  It does not select a vendor or authorize this application to perform a write.

## MCD discovery agenda

Ask MCD to provide one owner-approved system diagram, one row per insurance
source, and synthetic examples for create/update/termination/correction cases.
The first adapter should be selected by clinical-operational value and contract
clarity—not by which source is easiest to scrape. Freeze its mapping fixture,
run first import/replay/rollback acceptance, then onboard the next source.
