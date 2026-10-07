# Historical keypad outcome labels — Tier 2

Show already-recorded `no_input` and `invalid_input` call dispositions in the
staff history/review label map. Previously the missing labels hid these events
from the keypad-response list and left the staff review entry unexplained.

Scope: two display labels and a focused contract regression proving that both
events remain reviewable while a texting-only workspace still prohibits calls.
No voice activation, vendor request, consent/dispatch change, new data, migration,
case-state transition, or suppression change. Based on the delivery-error branch
so its shared communication formatter changes remain intact.

Validation: reproduce the missing labels in the new focused test, then run the
communication projection/review contract tests. No builds or broad suites.
Parent independently reviews the final commit before publication/merge.

Completed: the new regression first failed with `[null, null]` labels. After
adding the two labels, `node --test src/caseCommunications.test.js
src/communicationsEventReview.test.js` passed all 28 tests. Voice actions remain
disabled in the texting-only regression. No vendor requests or builds ran.

Recovery: revert this label-only commit; existing call events and their staff
review state remain unchanged.
