# Shared SMS inbox and queue counts

Tier 2: staff review of messages and attribution to patient cases.

Build on PR70’s repaired case workspace. Port the complete preserved inbox,
strict response/command contracts, bounded count refresh helper and their tests
from 8d0caca. Mount the inbox only for an explicit server worklist capability,
including an empty queue. An unknown sender is never assigned automatically.
Staff explicitly select and confirm a current-contact match or leave it
unassigned; reviewed STOP events remain suppressed. Preserve exact retry IDs.

While visible, refresh only per-case and unassigned counts every 15 seconds.
Preserve selected case, draft, queue order and newer local review decisions.
Pause polling during active/uncertain changes and hide it when the page is hidden.
Read failures retain the previous counts with an explicit stale message.

Validation: complete preserved Node contracts and two browser scenarios on the
built React application with synthetic local HTTP fixtures; affected existing
SMS browser checks; build and required unchanged clinical guards. Real shared
PostgreSQL API/callback acceptance is covered in the separate backend stack,
with joint integration following. No vendor, model, staging or live-SMS changes.

Require independent non-implementer review of the final pushed commit before
merge. Recover by reverting this unactivated UI slice; no records or schema
are removed. Server-side authorization remains mandatory.
