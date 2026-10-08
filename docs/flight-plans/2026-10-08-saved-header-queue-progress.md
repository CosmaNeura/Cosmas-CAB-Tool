# Saved header queue progress

Risk: **Tier 2**, because the read-only worklist reports patient-workspace review
metadata under existing actor, practice and source controls. Base:
`7af07ea316161ec87775c4348978f660616b025f`.

## Outcome

Provider and coder queues show the persisted current header revision and whether
that exact revision has a coder receipt. Browser-session review/processing labels
remain separate. No header, code, clinical text or actor identity enters the list
summary. A current-source miss means no current header; stale/corrupt/unavailable
reads mean unavailable, and membership denial retains its authorization failure.

The additive closed `billing-header-progress.v1` summary binds encounter version,
bundle digest and provenance. Header-disabled list responses remain unchanged.
Reads use existing source locks, membership/RLS and payload checksum validation;
no schema, grant, inference, outreach or billing-decision changes are needed.

## Staff behavior and checks

Reload reads persisted status. A validated header read/save/signoff updates that
row; dirty, pending, unknown or invalidated work masks it as unavailable. Explicit
queue refresh is blocked by dirty/pending work, never remounts the selected editor,
and cannot overwrite a newer editor receipt. Authorization failure uses the
existing app sign-in/access boundary. Queue metadata does not authorize editing,
generation or signoff, and never marks a claim submitted.

Validate golden revision-zero receipts; save/signoff/edit/current-source changes;
corrupt and unavailable reads; membership denial; header-off compatibility;
strict frontend parsing and stale-response ownership. One built browser fixture
covers reload, dirty-editor retention and held-save/late-refresh order. Retain
guardrail fuzz and anti-upcode evidence. No hosted or real-patient proof is claimed.

## Rollout and recovery

First intended cohort is the existing twelve synthetic training cases. Hosting,
identity, model and operational approvals remain separately governed. No current
preview or staging runtime is changed by source landing. Existing image11 remains
a separate, already-reviewed SMS candidate.

This behavior follows the existing header-enabled capability. Recover by serving
the previous reviewed application artifact, preserving header/revision/signoff
history and all current runtime permissions. No data deletion, migration reversal,
outreach activation or automatic retry is part of recovery. Independent review
must bind the final substantive pushed commit before normal merge.
