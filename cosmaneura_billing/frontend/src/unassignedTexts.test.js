import assert from "node:assert/strict";
import test from "node:test";
import { readUnassignedCount, readUnassignedTexts, readUnassignedCases, unassignedCommand, readUnassignedReceipt } from "./unassignedTexts.js";

const at = "2026-09-30T16:00:00.123456+00:00";
const uuid = "11111111-1111-4111-8111-111111111111";
const item = { ref: "a".repeat(64), sourceHash: "b".repeat(64), kind: "sms_reply", channel: "sms", text: "Synthetic response",
  at, status: "received", disposition: "reply_received", mediaCount: 0, senderHint: "Ending 0177", reviewState: "pending", reviewedAt: null, attributedCaseId: null };
const page = { schemaVersion: "communications-unassigned.v1", state: "pending", pendingCount: 1, items: [item], hasMore: false, nextCursor: null };
const candidate = { chaseId: "synthetic-case", patientLabel: "Synthetic patient", state: "needs_review", bucket: "missing", contactSnapshotId: uuid, verifiedAt: at };
const choices = { schemaVersion: "communications-unassigned-cases.v1", eventRef: item.ref, sourceHash: item.sourceHash, items: [candidate], hasMore: false, nextCursor: null };

test("valid bounded display drops routing/vendor extras and preserves exact text", () => {
  assert.equal(readUnassignedCount({ schemaVersion: "communications-unassigned-count.v1", pendingCount: 53 }), 53);
  const result = readUnassignedTexts({ ...page, items: [{ ...item, text: "😀\nSTOP", sender: "+12025550177", accountSid: "secret" }] }, "pending");
  assert.equal(result.items[0].text, "😀\nSTOP");
  assert.equal(result.items[0].sender, undefined); assert.equal(result.items[0].accountSid, undefined);
  assert.deepEqual(readUnassignedCases(choices, item).items, [candidate]);
});

test("unverified shapes, states, bounds and source changes fail closed", () => {
  for (const changes of [{ pendingCount: -1 }, { state: "reviewed" }, { hasMore: true }, { nextCursor: "unexpected" },
    { items: [item, item] }, { items: [{ ...item, reviewedAt: at }] }, { items: [{ ...item, attributedCaseId: candidate.chaseId }] },
    { items: [{ ...item, senderHint: "+12025550177" }] }, { items: [{ ...item, text: "\ud800" }] },
    { items: [{ ...item, text: "x".repeat(1601) }] }, { items: [{ ...item, mediaCount: 11 }] },
    { items: [{ ...item, at: "2026-02-30T16:00:00Z" }] }]) assert.throws(() => readUnassignedTexts({ ...page, ...changes }, "pending"));
  assert.throws(() => readUnassignedCases({ ...choices, sourceHash: "c".repeat(64) }, item));
  assert.throws(() => readUnassignedCases({ ...choices, items: [{ ...candidate, contactSnapshotId: "wrong" }] }, item));
  assert.throws(() => readUnassignedTexts({ ...page, state: "reviewed" }, "reviewed"));
});

test("a single matching case never becomes an implicit confirmed assignment", () => {
  for (const args of [[item, "attribute", null, true, uuid], [item, "attribute", candidate, false, uuid],
    [item, "leave_unassigned", candidate, true, uuid], [{ ...item, reviewState: "attributed" }, "attribute", candidate, true, uuid]]) {
    assert.throws(() => unassignedCommand(...args));
  }
  assert.deepEqual(unassignedCommand(item, "leave_unassigned", null, true, uuid), { actionId: uuid, eventRef: item.ref,
    sourceHash: item.sourceHash, decision: "leave_unassigned", confirmed: true, chaseId: null, contactSnapshotId: null });
});

test("success requires an exact receipt, including actor-bound action and selected case", () => {
  const cmd = unassignedCommand(item, "attribute", candidate, true, uuid);
  const value = { schemaVersion: "communications-unassigned-decision.v1", pendingCount: 0, receipt: { ...cmd, reviewedAt: at } };
  assert.deepEqual(readUnassignedReceipt(value, cmd), { pendingCount: 0, reviewedAt: at });
  for (const key of ["actionId", "eventRef", "sourceHash", "decision", "chaseId"]) {
    assert.throws(() => readUnassignedReceipt({ ...value, receipt: { ...value.receipt, [key]: "wrong" } }, cmd));
  }
  assert.equal(Object.isFrozen(cmd), true);
});

test("reviewed rows distinguish attribution from global completion without changing source", () => {
  for (const [reviewState, attributedCaseId] of [["attributed", candidate.chaseId], ["left_unassigned", null]]) {
    const result = readUnassignedTexts({ ...page, state: "reviewed", pendingCount: 0,
      items: [{ ...item, reviewState, attributedCaseId, reviewedAt: at }] }, "reviewed");
    assert.equal(result.items[0].text, item.text); assert.equal(result.items[0].attributedCaseId, attributedCaseId);
  }
});
