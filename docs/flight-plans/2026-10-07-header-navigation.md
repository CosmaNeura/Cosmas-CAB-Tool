# Preserve embedded billing-header edits during navigation

Risk: Tier 2, billing-review workflow. The main provider and coder workspaces
could unmount a dirty header or an unresolved save while changing visits/tabs.
The standalone header already protected its own refresh action.

The workspace now receives header dirty/request state, asks before discarding
unsaved edits, and blocks case/workspace/handoff navigation until a pending
mutation is resolved. A mutation announces its navigation lock before network I/O.
Read-only loads remain cancelable by navigation, including an unanswered read;
ordinary header/coder loading controls still receive their full busy state.
Confirming a discard checks the current state again; beginning or completing a
save clears an earlier discard prompt. Browser reload/close warns for unsaved
edits and unresolved operations. Existing frozen coder-review controls remain.

No backend, clinical note, code decision, source admission, schema, consent,
outreach, or deployment configuration changes. No feature activation.

Validation: focused browser checks use the built application with synthetic HTTP
fixtures for provider and coder drafts, canceled/discarded navigation, lost-save
exact retries, stale discard prompts, successful saves, and reload warnings.
The standalone header retry checks remain applicable and are run once alongside
the new cases because mutation-state notification is shared. These checks do
not claim real backend persistence or hosted acceptance.

Recovery: revert this frontend change and rebuild the previous reviewed source;
no data/schema rollback. Never navigate away from an unresolved request merely
to clear its lock. Independent final-commit review is recorded before merge.
