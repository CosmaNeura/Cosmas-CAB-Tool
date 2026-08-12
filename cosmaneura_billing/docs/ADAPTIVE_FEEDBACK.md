# Adaptive feedback — how reviewer corrections re-enter the engine

Every accept / edit / reject a reviewer makes in the coder-review UI is stored
(`coder_feedback.py`, JSONL, append-only). Two things consume that log:

1. **The tuning backlog** (`scripts/analyze_feedback.py`, `/api/feedback/summary`)
   — ranked rule/prompt change candidates and the growing gold set. These feed
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

## Reproducibility

Preferences are derived deterministically from the append-only feedback log
and cached on the log's mtime — a submitted review takes effect on the next
pipeline run, no restart. Given the same log, every process derives the same
engine behavior; deleting the log returns the engine to base behavior. The log
itself is the audit record of *why* the engine behaves differently for a user.

## Where identity comes from

The API accepts the reviewer identity as `userId` in `POST /api/billing`, or
`?user_id=` on the stored-encounter and coder-view endpoints; the coder-review
UI passes the coder id it displays. In production this should come from the
authenticated session, not the client payload.
