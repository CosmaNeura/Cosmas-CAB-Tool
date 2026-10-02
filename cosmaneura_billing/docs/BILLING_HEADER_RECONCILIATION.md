# Reconcile earlier header suggestions

A saved header pins an immutable source and original pipeline result. A later
successful coder-view run can return a different recommendation manifest for
that same source. A current-manifest review alone does not review an original
header code that is absent from that run.

The signoff route now accounts for every original `(kind, code)` either through
the current server-signed manifest's explicit code decision, or through an
explicit earlier-code decision. Human header edits retain their independent
proposal review. Neither generation nor reconciliation replaces the original
header, source, edit history, or prior receipt.

`POST /api/encounters/header/signoff` accepts an additive optional array:

```json
{"baselineDecisions":[{"id":"original-header-code-id","decision":"reject","reason":"Coder's review rationale"}]}
```

The array must cover exactly the original header IDs whose kind/code pairs are
absent from the current signed manifest. It has at most 20 entries; each reason
is required and bounded to 1,000 characters. Unknown/duplicate IDs, missing or
extra decisions, and outcome conflicts fail before persistence.

- `reject`: that original kind/code must be absent from the final code outcomes.
- `retain`: that original kind/code must be present in an ordinary human outcome.
  Selecting retain does not add anything. The coder separately enters a manual
  addition with rationale/evidence, or edits the current E/M. If no current E/M
  exists, an explicit manual E/M addition is supported by the existing feedback
  API; the limit of one E/M decision per review remains unchanged.
- If the current manifest is empty, the coder may explicitly reject all earlier
  codes and record an empty set of final codes. The ordinary empty/no-review UI
  remains blocked unless this explicit baseline reconciliation is complete.

The existing signed current manifest is never expanded or replaced with earlier
codes. Original quotes remain source-validated evidence, not new engine approval
or proof that a retained code is medically supported. Clinical review is human.

The receipt records `baselineDecisions` with the unchanged current-manifest
digest, code decisions, human proposal decisions, actor, source and revision.
The existing store transaction commits coder feedback, receipt and authenticated
access-audit completion together, or none. Retries bind all decisions/reasons to
the original review ID. No SQL schema change is needed: the existing immutable
receipt JSON stores the additive field. Legacy fully covered receipts remain
readable and exactly replayable; a receipt that omitted an original code cannot
be displayed as fully reviewed without reconciliation evidence.

The browser displays earlier suggestions separately, requires decisions/reasons,
and shows the recorded choices in the header and text export. A failed outcome
consistency check leaves the draft editable. An ambiguous save freezes the same
request for exact retry, including the reconciliation choices.

## Local verification

From `cosmaneura_billing/`:

```sh
.venv/bin/python -m pytest -o addopts= -q tests/test_billing_header_signoff.py tests/test_billing_header_signoff_api.py
# Set BILLING_HEADER_TEST_DSN only to an explicit disposable local cab_header_test database:
.venv/bin/python -m pytest -o addopts= -q tests/test_billing_header_signoff_postgres.py
```

From `cosmaneura_billing/frontend/`:

```sh
node --test src/billingHeader.test.js src/coderReview.test.js
```

The original isolated browser fixture is preserved on the feature branch, not
ported here. This integration uses focused source/API checks and a disposable
PostgreSQL rehearsal with synthetic fixtures and no live inference.

This Tier 2 correction is covered by the current header-signoff integration
flight plan. It does not change deployment, model, consent, outreach, Athena,
EHR-write or claim-submission activation.
