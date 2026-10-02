# Athena staff review UI contract

Tier 2: validation of patient/source identity and imported clinical context.

Preserve the complete final Athena frontend contract and its unmodified golden
backend fixture from 8d0caca. It validates explicit case/practice/patient mapping,
revisions, exact response shapes, staff-selected contact provenance, unknown
consent, bounded pagination and the reviewed note/context receipt.

This contract and large fixture form the first bounded UI dependency. The
following mounting slice adds the complete two preserved Node suites and actual
React component with browser acceptance. For this commit, execute both full
suites from local evidence with only their import/fixture URLs redirected to
this candidate; retain their original bytes and adaptation binding. No assertion
is omitted. The fixture stays readable and byte-identical rather than compressed
to alter review size. Required guardrails also run once.

No UI, API, setting, source data or deployed state changes yet. Independent final
commit review required before merging. Revert this dormant contract/fixture
without changing stored records. No vendor/model calls or clinical acceptance
claim; the later UI appears only for server-advertised capability.
