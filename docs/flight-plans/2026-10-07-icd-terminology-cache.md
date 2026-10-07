# Verified bundled ICD terminology index

Tier 1: reversible runtime acceleration with a generated public-data artifact.
Clinical terminology, evidence, coding rules and model authority do not change.

The installed image07 worker proof timed out before terminal headers. A separate
profile measured 106 seconds in the cold ICD index, predominantly descriptor
normalization. This outcome moves only that deterministic work to a reproducible
offline generation step. It does not claim a completed worker latency proof.

The generator reuses the existing effective-current-FY loader and index builder.
It preserves positive-token order, negative sets, complete ambiguous code lists,
code order and index-key order. The gzip JSON contains public CMS descriptors'
derived terms and codes, never encounter data. Its timestamp is zero.

Runtime verifies the reviewed compressed payload hash, exact catalog and
terminology-source hashes/version, format/generator identity, ordered catalog
digest, bounded structures and complete unique code coverage. Only the bundled
current catalog is eligible. Custom, missing, changed or malformed inputs use
the original index builder; runtime never writes or repairs an artifact.

Generation is explicit, local and default-inert. Root generated the artifact
once in 2.753 seconds, with 74,719 billable entries, 74,703 keys and 16 ambiguous
keys; the full ordered index round trip matched. The 554,898-byte gzip expands
to 7,713,924 bytes and is pinned as
`3d22f133d04086f4e8faea87d961d22d00892bc4627eda02948b1ff4b83c89ca`.
This host generation timing is not a runtime or Linux latency result. The final
PR includes this generated artifact. No CI,
Dockerfile, dependencies, auth, schema, inference settings or model calls change.

Verification: full-current-catalog equality through the actual fast path;
ambiguity/order, source invalidation and bounded corrupt-input fallback cases;
existing terminology tests and guardrail invariants. These are pending until
root's actual runs; the final image still needs installed-worker acceptance.

Recovery: remove the cache file or revert this PR to restore the existing cold
builder. No persistent claims, headers, audit, clinical source or database data
are changed or removed. Independent source review precedes publication.
