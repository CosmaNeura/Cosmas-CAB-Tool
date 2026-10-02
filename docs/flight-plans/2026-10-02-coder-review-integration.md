# MCD release integration: explicit coder decisions

Tier 2. Integration base: `01ad97536c4f6730e30cd8a03dd31cdb07a918fb`.

## Outcome

Bring the existing explicit coder-review workflow onto current main as one
reviewable change. The completed feature branch at `8d0caca` is preserved.
The user authorized continuous CAB implementation and local-agent validation;
MCD's first cohort is synthetic training. This change does not deploy or enable
patient outreach, Athena requests, model services, or real patient data.

## Scope and invariants

- Start from the nine coder-review API/UI/test files in `3d051a5`, taking the
  three corrected CoderReview/helper/test files from `0502ef8` as specified by
  the retained source-integration plan. Keep the completeness-save fix.
- Coders explicitly accept, reject or replace suggestions. A review cannot
  silently accept untouched rows, attribute an action to a caller-supplied
  identity, or turn an incomplete encounter into a prepared one.
- Preserve exact retries, stale-context rejection, role isolation, note
  immutability, evidence-linked suggestions and the existing billing pipeline.
- Bring forward the existing v3 server-secret signing fix from 0e16115.
  Encounter identifiers must never serve as signing keys. Durable/deployed
  replicas require the same configured secret; local-only ephemeral keys are
  permitted for development/test. Missing keys fail closed.
- Review context binds the exact source/result and actor; it is not clinical
  approval. Reject old v2 and forged, altered or cross-actor/case contexts.
- Existing GitHub workflow bytes, infrastructure, schema and feature source
  branch stay unchanged. No hosted configuration or data changes are included.

## Verification and recovery

Run scoped API/security/coder helper checks on this reconstructed tree, plus
required billing guardrail/consensus invariants. Check the frontend compiles.
Use an independent local reviewer for the final source and bind the review to
the resulting commit; historical reviews are supporting evidence only.

Require a fresh review context after a deployment/reload when its result or
encounter version changes; reject stale review submissions instead of adapting
client decisions. Retry only the identical submitted review with its same ID.
Recovery uses a compatible API/UI image retaining server-secret signing;
if none exists, keep the cohort closed and fix forward. Retain existing review
history. Do not roll back populated data or database structures.

This source slice is not the complete CAB release. Final main must include all
required source, schema and runtime dependencies before hosted promotion.
