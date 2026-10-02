import test from "node:test";
import assert from "node:assert/strict";
import { eventReviewCommand, readEventReview, reviewCounts, reviewDecisions } from "./communicationsEventReview.js";

const chaseId = "11111111-1111-4111-8111-111111111111";
const actionId = "22222222-2222-4222-8222-222222222222";
const snapshot = "33333333-3333-4333-8333-333333333333";
const item = () => ({ ref: "a".repeat(64), sourceHash: "b".repeat(64), kind: "sms_reply", channel: "sms",
  text: "Synthetic reply: please call.", at: "2026-09-29T16:00:00.123456+00:00", status: "received",
  disposition: "reply_received", held: false, canAttribute: false, contactSnapshotId: null,
  reviewState: "pending", mediaCount: 0 });
const fixture = () => ({ schemaVersion: "communications-review.v1", chaseId, totalPending: 1, heldPending: 0,
  items: [item()], truncated: false });

test("projects only selected-case review fields and counts, including Unicode text", () => {
  const value = fixture();
  value.secret = "omit"; value.items[0].providerSid = "omit"; value.items[0].text = "Synthetic ✅\u007f";
  const result = readEventReview(value, chaseId);
  assert.equal(result.items[0].text, "Synthetic ✅\u007f");
  assert.equal(result.secret, undefined); assert.equal(result.items[0].providerSid, undefined);
  assert.deepEqual(reviewCounts({ pending: 3, held: 2, source: "omit" }), { pending: 3, held: 2 });
  for (const counts of [null, {}, { pending: NaN, held: 0 }, { pending: 0, held: 1 }, { pending: -1, held: 0 }]) assert.equal(reviewCounts(counts), null);
});

test("counts are full totals with an exact next-50 bounded window", () => {
  const value = fixture(); value.totalPending = 60; value.heldPending = 10; value.truncated = true;
  value.items = Array.from({ length: 50 }, (_, index) => ({ ...item(), ref: index.toString(16).padStart(64, "0") }));
  assert.equal(readEventReview(value, chaseId).totalPending, 60);
  value.heldPending = 11;
  assert.throws(() => readEventReview(value, chaseId)); // 50 assigned cannot fit 49 assigned total.
});

test("all decisions are explicit; held attribution pins the exact contact and source", () => {
  const value = fixture();
  let view = readEventReview(value, chaseId);
  assert.deepEqual(reviewDecisions(view.items[0]), ["handled"]);
  for (const choice of ["", "attribute", "leave_unassigned"]) assert.throws(() => eventReviewCommand(view, item().ref, choice, true, actionId));
  assert.throws(() => eventReviewCommand(view, item().ref, "handled", false, actionId));
  assert.throws(() => eventReviewCommand(view, item().ref, "handled", true, "bad-id"));
  value.heldPending = 1;
  Object.assign(value.items[0], { held: true, canAttribute: true, contactSnapshotId: snapshot });
  view = readEventReview(value, chaseId);
  assert.deepEqual(reviewDecisions(view.items[0]), ["attribute", "leave_unassigned"]);
  const command = eventReviewCommand(view, item().ref, "attribute", true, actionId);
  assert.deepEqual(command, { chaseId, actionId, eventRef: item().ref, sourceHash: item().sourceHash,
    decision: "attribute", confirmed: true, contactSnapshotId: snapshot });
  assert.ok(Object.isFrozen(command));
  assert.equal(eventReviewCommand(view, item().ref, "leave_unassigned", true, actionId).contactSnapshotId, null);
  assert.throws(() => eventReviewCommand(view, item().ref, "handled", true, actionId));
  Object.assign(value.items[0], { canAttribute: false, contactSnapshotId: null });
  assert.deepEqual(reviewDecisions(readEventReview(value, chaseId).items[0]), ["leave_unassigned"]);
});

test("attribution still requires a separate handled review; no send or consent fields exist", () => {
  const value = fixture(); value.items[0].reviewState = "attributed";
  const view = readEventReview(value, chaseId);
  assert.equal(view.totalPending, 1);
  assert.deepEqual(reviewDecisions(view.items[0]), ["handled"]);
  assert.deepEqual(Object.keys(eventReviewCommand(view, item().ref, "handled", true, actionId)).sort(),
    ["actionId", "chaseId", "confirmed", "contactSnapshotId", "decision", "eventRef", "sourceHash"]);
});

test("assigned keypad and delivery failure records are accepted without invented message bodies", () => {
  const value = fixture();
  Object.assign(value.items[0], { kind: "call_keypad", channel: "automated_voice", text: "", disposition: "stop_calls", status: "completed" });
  assert.equal(readEventReview(value, chaseId).items[0].disposition, "stop_calls");
  Object.assign(value.items[0], { kind: "delivery_failure", disposition: null, status: "failed" });
  assert.equal(readEventReview(value, chaseId).items[0].kind, "delivery_failure");
});

test("malformed, stale, cross-case, oversized or inconsistent DTOs fail closed", () => {
  const changes = [
    (v) => { v.chaseId = actionId; }, (v) => { v.schemaVersion = "other"; },
    (v) => { v.totalPending = -1; }, (v) => { v.totalPending = 2; },
    (v) => { v.heldPending = 1; }, (v) => { v.truncated = true; },
    (v) => { v.items.push(v.items[0]); v.totalPending = 2; },
    (v) => { v.items[0].ref = "A".repeat(64); }, (v) => { v.items[0].sourceHash = ""; },
    (v) => { v.items[0].text = "x".repeat(1601); }, (v) => { v.items[0].text = "\ud800"; },
    (v) => { v.items[0].mediaCount = 11; },
    (v) => { v.items[0].at = "2026-02-30T12:00:00Z"; },
    (v) => { v.items[0].at = "2026-09-29T12:00:00-00:00"; },
    (v) => { v.items[0].at = "2026-09-29T12:60:00Z"; },
    (v) => { v.items[0].channel = "automated_voice"; },
    (v) => { v.items[0].canAttribute = true; v.items[0].contactSnapshotId = snapshot; },
    (v) => { v.items[0].contactSnapshotId = snapshot; },
    (v) => { v.items[0].kind = "delivery_failure"; },
    (v) => { v.items[0].reviewState = "handled"; },
  ];
  for (const change of changes) { const value = fixture(); change(value); assert.throws(() => readEventReview(value, chaseId)); }
});

test("empty queue is an explicit verified zero; missing data is unavailable", () => {
  assert.throws(() => readEventReview(null, chaseId));
  const value = fixture(); Object.assign(value, { totalPending: 0, items: [] });
  assert.equal(readEventReview(value, chaseId).totalPending, 0);
});
