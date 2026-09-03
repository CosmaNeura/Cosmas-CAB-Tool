# Scribe → CAB de-identified visit ingestion contract — v1

`contract_version: scribe.visit.v1`

This is the receiver-first contract a producer (Scribe first, any de-identified
source second) targets to hand CosmaNeura CAB finished visits for the provider
suggested-CPT pipeline. The receiver is
`cosmaneura_billing.scribe_import.ingest_visit_page`, which normalizes each
record and commits the page through the durable, idempotent
`cosmaneura_billing.source_import` layer.

## Boundaries

- **De-identification is the producer's responsibility.** This adapter validates
  shape and normalizes; it performs no scrubbing and asserts no PHI gate is
  satisfied. Until every PHI/BAA/deployment gate is complete, producers send
  **synthetic or properly de-identified** text only.
- **Provenance is `scribe_deidentified`.** It is deliberately *not* one of the
  reserved synthetic-demo provenances, so ingested encounters never masquerade
  as the fixed demo pack and the synthetic-only inference latch is unaffected.
- **PostgreSQL acceptance is server-owned.** In staging/production the
  `cosmaneura_source_import_allowed(practice_id, source_system, provenance)`
  binding governs whether a page commits; this contract cannot widen it.
- **Fail-closed.** Any malformed page is rejected whole with a static,
  identifier-free `SOURCE_IMPORT_SCHEMA_INVALID` error. Nothing is partially
  imported.

## Page shape

```json
{
  "contract_version": "scribe.visit.v1",
  "batch_id": "<opaque, stable per batch, ≤256 chars, no control chars>",
  "records": [
    {
      "source_record_id": "<opaque de-identified stable id, 1–256 chars>",
      "source_version": "<opaque version/hash of this record at source>",
      "visit": {
        "encounter_type": "office_established",
        "place_of_service": "11",
        "patient_age": 26,
        "patient_sex": "M",
        "signed_note": "<required, non-empty: billing is derived from this>",
        "intake_text": "<optional>",
        "transcript": "<optional>",
        "provider_specialty": "Family Medicine",
        "submitted_em_code": "99213",
        "submitted_icd10": ["K62.5"]
      }
    }
  ]
}
```

### Field rules

| Field | Rule |
|---|---|
| `contract_version` | must equal `scribe.visit.v1` |
| `batch_id` | non-empty, trimmed, ≤256 chars, no control characters |
| `records` | non-empty JSON array; unknown record keys rejected |
| `source_record_id` / `source_version` | non-empty strings, 1–256 chars, trimmed, no control characters |
| `visit.encounter_type` / `place_of_service` / `patient_sex` / `provider_specialty` | non-empty label, ≤64 chars, trimmed, no control characters |
| `visit.patient_age` | integer `0–130` (booleans rejected) |
| `visit.signed_note` | **required, non-empty**, ≤200,000 chars |
| `visit.intake_text` / `transcript` | optional string, ≤200,000 chars |
| `visit.submitted_em_code` | optional string label or `null` |
| `visit.submitted_icd10` | optional array of ≤64 string codes |

Unknown keys anywhere in the page, a record, or a visit fail the whole page.

## Idempotency, replay, and ordering

- The receiver derives the durable idempotency key from
  `(practice_id, source_system="scribe", feed="encounters", source_record_id,
  source_version, mapping_version="scribe.visit.v1")`.
- **Re-posting the same `batch_id` with the same records is a pure replay:**
  `imported_count == 0`, `replayed_count == len(records)`, and the checkpoint
  does not advance.
- **A changed payload under the same `source_record_id` + `source_version`
  conflicts** (`SOURCE_IMPORT_CONFLICT`) — bump `source_version` to update.
- Optimistic concurrency is enforced by the store's checkpoint version; a
  concurrent writer fails closed rather than clobbering.

## Return value

`SourceImportPageResult(imported_count, replayed_count, checkpoint)` — counts
only. No patient text, source id, or cursor is returned or logged.

## Versioning

`contract_version` gates the wire shape. A future `scribe.visit.v2` may be
introduced additively without breaking v1 producers; the receiver validates the
exact version it supports and rejects the rest.
