# Adaptive feedback — how reviewer corrections re-enter the engine

Every accept / edit / reject a reviewer makes in the coder-review UI is stored
as an append-only domain record. Staging and production use normalized,
practice-scoped PostgreSQL tables; JSONL is retained only for local
development/tests and explicitly reviewed legacy import. Two things consume
that history:

1. **The tuning backlog** (`scripts/analyze_feedback.py` for an explicit local
   export, `/api/feedback/summary` for the configured store) — ranked
   rule/prompt change candidates and the growing gold set. These feed
   *versioned releases*: a human reviews them, changes rules or prompts, and the
   change is regression-tested against the frozen gold set before shipping
   (VALIDATION_PROTOCOL.md §11, §13).
2. **The runtime preference layer** (`preferences.py`) — the subject of this
   document: adjustments applied automatically on the reviewer's *next* run,
   under hard constraints.

## The compliance line

The engine's core promise is that it is structurally incapable of upcoding.
Learned behavior must not create a side door, so the preference layer obeys
one rule: **corrections can make the engine more conservative or more
specific, never more lucrative.**

- It can **hold** a code for review. It never auto-drops it (the reviewer
  still sees it, with the history) and never auto-adds one.
- It can **refine** a diagnosis to the more specific code the reviewer keeps
  choosing — only within the same 3-character ICD-10 family, only if the
  target is billable in the current FY code set, and the original evidence is
  preserved.
- It **never moves an E/M level.** Repeated leveling disagreements become an
  informational note on the suggestion and a tuning-backlog item; levels only
  change through versioned rule releases.
- It runs **before the compliance guardrail**, which independently enforces
  all of the above (a `preference_hold` can only force REVIEW; the guardrail's
  keep/downgrade/remove invariant is untouched).
- Every applied preference writes an **audit line with provenance**:
  `HOLD K59.00 (...): you have declined K59.00 3x (not supported by
  documentation); held for review, not auto-dropped.`

## What is learned, and the evidence bar

| Signal | Trigger | Effect on future runs |
|---|---|---|
| Suppression | same reviewer rejects code X with reason *not supported* / *contradicted* ≥ 3 times, and has rejected it more often than accepted it | X is proposed but HELD for that reviewer, history attached |
| Refinement | same reviewer recodes X → Y (same ICD-10 family) with reason *needs a more specific code* ≥ 3 times | engine proposes Y directly for that reviewer, evidence unchanged, provenance in the rationale |
| E/M note | reviewer re-levels E/M with a leveling reason ≥ 3 times | informational note on the E/M suggestion; backlog item |

**Practice-wide** ("…and potentially others"): the same suppression or
refinement applies to *every* reviewer once at least **3 distinct reviewers**
have produced the signal (and, for suppression, population rejects outnumber
accepts). Per-reviewer signals always take precedence over practice-wide ones.

Thresholds: `COSMANEURA_PREF_MIN` (default 3) and
`COSMANEURA_PREF_GLOBAL_USERS` (default 3). `COSMANEURA_PREFS=0` disables the
layer. For a live demo, `COSMANEURA_PREF_MIN=1` shows the loop closing after a
single correction.

## Submission and idempotency

The client creates one UUID `review_id` when a review draft starts, freezes the
first submitted payload, and reuses both for ambiguous retries. It also echoes
the opaque, server-issued review context from the coder view. That context is
bound to the internal encounter UUID, encounter version, reviewer identity,
practice, and engine version, so another replica can verify it during a rolling
deployment without trusting client-authored metadata. The server supplies
reviewer identity and engine version; clients cannot assert either. PostgreSQL
enforces one review ID per practice and stores a canonical payload hash:

- The first submission creates the review and returns `replayed: false`.
- An exact retry returns success with `replayed: true` and creates no duplicate
  feedback or preference signal.
- Reusing the ID with a different payload returns 409 and changes nothing.
- Feedback for a superseded encounter version returns 409 and requires a fresh
  review, so corrections cannot be attributed to documentation the coder did
  not actually review.

The access-audit attempt is written before the route runs. In staging and
production the feedback rows and successful audit completion commit in one
PostgreSQL transaction, so a failed completion rolls back the review. A review
also requires an existing encounter and an active explicit practice membership.

## Reproducibility

Preferences are derived deterministically from the append-only feedback
history. In PostgreSQL the preference cache keys off the practice's monotonic
feedback revision, so a submitted review takes effect on the next pipeline run
across replicas without a restart. Given the same rows, every process derives
the same behavior. In local JSONL mode only, the cache uses the file's revision;
deleting that local file returns the engine to base behavior. The stored
feedback and decision provenance explains *why* the engine behaves differently
for a user.

## Where identity comes from

The API derives reviewer identity only from the Microsoft Entra session
validated by Azure Container Apps built-in authentication. The immutable Entra
object ID keys feedback and preferences; the allowed tenant and application
role are validated and intersected with an active, explicit PostgreSQL practice
membership before the engine runs. Login never creates that membership.
`userId`, `coder_id`, `actor`, `?user_id=`, and `?coder_id=` are explicitly
rejected so an older client cannot silently believe that an identity spoof
succeeded. See `ACCESS_CONTROL.md`.

## Legacy JSONL import

Legacy feedback is not loaded implicitly. An operator must first prepare a JSON
mapping from every legacy caller-controlled `coder_id` label to a reviewed,
immutable Entra object UUID, then validate the entire file before writing:

    DATABASE_URL='<migration-owner DSN>' \
    COSMANEURA_ENTRA_TENANT_ID='<tenant UUID>' \
    COSMANEURA_PRACTICE_ID='<stable practice ID>' \
    python -m cosmaneura_billing.import_feedback legacy.jsonl \
      --coder-map reviewed-coder-map.json --dry-run

Remove `--dry-run` only after reviewing the validation count and file digest.
The importer rejects the whole batch for an invalid row, unknown mapping, or
uncontrolled decision; it does not auto-create identities. Deterministic review
IDs, source SHA-256, and line numbers make a rerun idempotent and preserve import
provenance. Keep source data and the mapping outside the repository and do not
print records or DSNs into logs.
