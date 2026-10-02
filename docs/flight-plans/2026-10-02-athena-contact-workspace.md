# Athena contact and insurance case workspace

Tier 2: source patient identity and staff-selected contact/insurance observations.

Use the reviewed Athena contract and golden fixture from the preceding branch.
Mount the complete preserved source-review component in CardChase when the
server advertises task.athenaReview.available. Keep its billing workflow behind
an explicit billingAvailable flag until the billing backend is integrated.

Staff reads only the stored mapping in configured mode and explicitly confirms
patient match, selected fields, insurance review and a reason. Successful apply
refreshes the case in place; it preserves the selected source tab. Ambiguous
writes keep their original operation IDs and lock destructive board refresh.
Contact application leaves permission unknown and does not complete the case.

Track both full preserved Node suites in this slice. Verify configured source
read/apply, missing mapping, unchanged case state, empty initial selections,
exact retry and hidden billing controls against the built UI with synthetic HTTP
fixtures, plus existing SMS regression cases. Backend shared-Postgres API proof
is separate; no live Athena/model request or staging/configuration change.

Independent final commit review required. Revert this UI mounting outcome while
retaining server-side source/audit records. Clinical source text is not edited.
