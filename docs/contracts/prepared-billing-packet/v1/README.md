# Prepared billing packet v1 draft contract artifacts

Status: **draft, synthetic-only, non-runtime**. These files do not authorize or
implement an API, database migration, feature activation, claim transmission,
Athena/EHR write, real-PHI use, patient outreach, or deployment. Implementation
remains gated by the Tier 2 flight plan in
[`../../../flight-plans/2026-08-26-cab-prepared-billing-packet.md`](../../../flight-plans/2026-08-26-cab-prepared-billing-packet.md).

## Files

- `contracts.schema.json`: self-contained schemas for the snapshot request,
  immutable review snapshot, prepare request, and immutable packet.
- `examples/*.golden.json`: wholly synthetic positive examples.
- `canonicalization-vectors.v1.json`: language-neutral byte and digest vectors.

The schema bundle uses a deliberately small, mechanically tested JSON Schema
profile and local `$ref` values only. The repository does not claim full JSON
Schema Draft 2020-12 validation without a declared validator dependency.

## Canonical JSON v1

Packet and snapshot canonicalization operates only on validated JSON
primitives:

```python
json.dumps(
    value,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
```

Implementations recursively reject floats, integers outside
`[-9007199254740991, 9007199254740991]`, non-ASCII object keys, duplicate keys,
lone Unicode surrogates, non-UTF-8 input, a BOM, trailing data, and values that
are not JSON primitives. Negative zero is prohibited. Booleans are checked
before integers because Python booleans are integer subclasses. Arrays retain
domain order; only object keys are sorted. Unicode is not normalized.

To seal an object, remove only its root `snapshotDigest` or `packetDigest`,
canonicalize the remaining root object, hash those exact bytes with SHA-256,
and add the lowercase hex digest. Nested digest fields remain in the hash input.
The complete sealed object is canonicalized again and its exact bytes are the
stored/downloaded representation. `replayed` belongs to an HTTP response
envelope, never to either immutable object.

The snapshot-request and prepare-request idempotency hashes are lowercase
SHA-256 over canonical JSON v1 for the entire strictly parsed, schema- and
domain-valid request. No request field is excluded; `elapsedMilliseconds` is
included and therefore frozen for exact retry. The checked-in vectors pin both
request hashes. A different canonical request under the same review ID is a
conflict rather than a replay.

For v1, `sourceSnapshotDigest` is canonical JSON v1 over the server-derived
object `{"encounterVersion": <integer>, "visit": <exact stored VisitBundle>}`.
The visit object has exactly the snake-case fields `encounter_id`,
`patient_age`, `patient_sex`, `encounter_type`, `place_of_service`,
`intake_text`, `transcript`, `signed_note`, `provider_specialty`,
`submitted_em_code`, and `submitted_icd10`. It is separate from the legacy
fixture case digest, which also binds pack and source provenance under the
fixture-manifest protocol below.

Array order is semantic:

- rulesets use lexical `(name, version)` order and names are unique;
- recommendations use contiguous snapshot ordinal;
- snapshot evidence uses first-reference order while walking recommendations,
  then completeness results; evidence references follow that catalog order;
- completeness results use lexical `ruleId` order and rule IDs are unique;
- evidence segments use ascending byte position and adjacent segments merge;
- decisions and excluded codes use snapshot recommendation order;
- manual additions use lexical client UUID order, and additional evidence uses
  the corresponding manual-addition order;
- final codes use snapshot recommendation order followed by manual additions;
  and
- guardrail actions retain deterministic pipeline order.

Duplicates and alternative orderings fail closed.

## Evidence segments

Offsets are zero-based, end-exclusive byte offsets into strict UTF-8 bytes of
the exact immutable source field. Boundaries must be code-point boundaries;
`text` must equal strict decoding of the selected bytes; and `textDigest`
hashes the raw selected bytes. Segments are ordered, non-overlapping,
non-adjacent, and bounded. A normalized or ellipsis match must uniquely
re-anchor to exact source bytes; ambiguous, repeated, unanchored,
out-of-bounds, or mid-code-point selections fail closed. One segment is at
most 8,192 UTF-8 bytes, and all evidence text in one snapshot is at most
65,536 UTF-8 bytes.

## Domain checks beyond the schema profile

Runtime validation must resolve every evidence and recommendation reference,
require one decision per recommendation in snapshot order, reject duplicate
IDs and duplicate final `(kind, code)` pairs, and derive packet reductions on
the server. Accept is valid only for a captured recommendation and preserves
its code/evidence exactly. Edit, reject, and manual addition require controlled
reason, nonblank rationale, and verified evidence; held or rejected engine rows
cannot be accepted. A degraded or completeness-blocked snapshot cannot prepare.
Packet version 1 has no predecessor; later versions reference the immediately
prior packet for the same encounter. Timestamps are canonical UTC and ordered.
Canonical snapshot and packet representations are limited to 256 KiB and
512 KiB respectively.

Every edited replacement and manual final code must resolve to at least one
evidence item with `purpose=supporting`; a source selection becomes supporting
evidence only after server verification and exact-byte re-anchoring. Final-code
records inherit only those validated references. Rejections may instead cite
`purpose=contradicting` evidence that supports the rejection rationale.

Packet v1 has exactly one decisioning inference path. It records
`multiEngineDecisioningApplied=false`; a non-influencing shadow output is not
included. Active consensus or any secondary decision influence blocks snapshot
issuance until a separately approved versioned provenance contract exists.

The positive packet example uses only the wholly synthetic, pack-declared
capture and exclusion for `SYN-RC-0001`. It is a contract artifact, not evidence
that the current offline mock model supports that deployable fixture.

## Separate fixture-manifest protocol

Fixture-manifest v1 is intentionally different. The existing owner-only seeder
uses sorted compact JSON with `ensure_ascii=true`, case input exactly
`{case_id,pack_id,source,visit}`, and combined input exactly
`{seed_id,packs,cases}`. A later implementation must expose that existing logic
through one seeder/packet manifest API without routing it through canonical JSON
v1 or changing any current `EXPECTED_*_DIGEST` value.

## Local validation

From `cosmaneura_billing`:

```bash
python -m pytest -v tests/test_prepared_packet_contract_artifacts.py
cd frontend && npm test
```

The test strictly parses every JSON file, checks the restricted schema profile,
validates positive examples and negative mutations, verifies segment bytes, and
pins the same checked-in canonical bytes and SHA-256 values in Python and Node
without adding a runtime dependency.
