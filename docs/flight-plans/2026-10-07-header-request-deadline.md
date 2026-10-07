# Bounded billing-header requests — Tier 2

A header mutation with no response previously stayed busy indefinitely and never
offered its exact retry. Give reads/saves a fixed 30-second client deadline and
generation a 300-second deadline, without any deployment/test override. Generation
can take the server's longer model budget; an ordinary read deadline must not
invite a premature generation retry. A mutation deadline preserves the
exact command and operation ID as an unresolved retry, keeps navigation locked,
and does not claim the server canceled or failed the operation. A read deadline
offers reload while navigation remains available.

Client abort does not cancel server work. Retrying the same operation preserves
server idempotency for saved revisions; concurrent generation retries can still
perform extra model work before persistence. This change does not claim otherwise
or change the server's generation execution/admission behavior.

Reuse request ownership for timer cleanup, unmount/supersession cancellation,
and late-result rejection. Timing out releases the expired request's ownership
before aborting; its later completion cannot replace a newer read or retry.
Normal cancellation does not create an ambiguous mutation or timeout message.

Scope: header request lifecycle and focused client tests only. No source
admission, coder frozen-review, backend idempotency, billing inference, auth,
schema, deployment, vendor, credential, or outreach activation changes.

Validation: request ownership/timer tests and built-browser provider/coder cases
advance Playwright's virtual clock through the real production deadline. Verify
exact retry payload, no duplicate operation, blocked mutation navigation, read
reload/unlocked navigation, canceled timers, and ignored late results. Use only
synthetic routed HTTP fixtures. No broad suites or vendor requests.

Recovery: revert this standalone client change and build the previously reviewed
source. Existing server operations remain idempotent and require exact retries;
no data/schema rollback. Independent review follows the final substantive push.

Validated locally on the reviewed source-aware workspace base
`aac12d45fd01b4bf17f4730c7c041577cc37edf1` (which includes navigation
`9337419d0dd5564f9377229d738cdfe203547a87`):

- `node --test src/billingHeader.test.js`: 36 passed, including exact command
  retention, stale ownership, timer cleanup, and the 300-second generation budget.
- `npm run build -- --configLoader runner`: passed. Runner loads the existing
  dependency cache without writing Vite configuration into the shared cache.
- `COSMANEURA_E2E_BASE_PORT=8872 ./node_modules/.bin/playwright test e2e/header-navigation.spec.js --grep 'timed-out|denied-model'`:
  6 passed against the built frontend. Four new provider/coder deadline cases
  plus both source-denied stored-header cases confirm the merged fixture still
  prevents inference while allowing exact save retries.
- `git diff HEAD --check`: passed. No backend, vendor, credential, or live
  outreach calls; browser HTTP responses are synthetic fixtures.
- Required billing guardrails: `python -m pytest -q tests/test_guardrail_fuzz.py`
  passed all 10 checks; `python prove_no_upcode.py` passed 4,000 adversarial
  guardrail/consensus cases. No clinical accuracy claim or engine change.
