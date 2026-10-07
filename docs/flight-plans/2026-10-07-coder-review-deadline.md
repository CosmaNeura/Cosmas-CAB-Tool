# Coder review confirmation deadline — Tier 2

An unanswered coder feedback or header-signoff request kept the workspace in
Saving indefinitely, with navigation locked and no usable exact retry. Reuse
the existing header request lifecycle and fixed 30-second ordinary mutation
deadline. No new timer configuration or network retry is introduced.

At the deadline, retire request ownership before aborting. Release only the
busy indicator; retain the frozen feedback, composed signoff body, review ID,
human decisions, source/revision binding and navigation/mutation lock. The coder
can explicitly retry those same bytes. A late response or cleanup cannot
replace the retry's state or claim a receipt. Client abort does not cancel
server work, and a timeout does not establish success or failure.

Scope is the client save lifecycle only. Billing evidence, source admission,
signoff validation, backend idempotence, schema, auth and outreach are unchanged.
Reuse existing tested request helpers rather than creating another framework.

Local checks: existing coder/header Node contracts, the existing frontend
Python contract file, production build and two built-browser regressions for
feedback and header signoff. Each browser case holds an actual response body
past abort, advances the real deadline, verifies frozen controls and exact-body
retry, then releases the expired body after confirmation. All responses are
synthetic fixtures. Required guardrail fuzz and 4,000 invariant cases also run;
no broad application suite, DB, credential, vendor or cloud action is needed.

Recovery is a revert of this client-only change followed by the normal reviewed
build process. No schema/data rollback or replacement review is performed.
Independent review must bind the final substantive commit; it does not approve
deployment. Existing final764 images remain immutable and do not include this
follow-on until a new source/image is explicitly prepared.

Verified locally against base `76435b03d55984518ee0713dbb29ff4f4ca99d4c`,
using the existing dependency cache without downloads:

- `node --test src/coderReview.test.js src/billingHeader.test.js`: 51 passed.
- `python -B -m pytest -q tests/test_synthetic_demo_frontend.py tests/test_guardrail_fuzz.py`:
  27 passed, including the 10 guardrail checks.
- `python -B prove_no_upcode.py`: 4,000 supplied-fact invariant cases passed.
- `node node_modules/vite/bin/vite.js build --configLoader runner`: passed.
- `COSMANEURA_E2E_BASE_PORT=8877 node node_modules/@playwright/test/cli.js test e2e/header-navigation.spec.js --grep 'coder (header signoff|feedback) deadline'`:
  both regressions passed on the first run, with zero retries or skips.

The browser checks exercise the built component and existing synthetic HTTP
fixtures. They do not repeat backend signoff/database acceptance or prove live
identity or delivery. The original final764 counterexample and exact command
logs are retained outside Git in the local evidence package.
