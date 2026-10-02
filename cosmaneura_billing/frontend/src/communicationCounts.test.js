import test from "node:test";
import assert from "node:assert/strict";
import {hasCommunicationCounts, listedCaseIds, mergeListedCounts, pollVisibleCounts, readListedCounts} from "./communicationCounts.js";

const counts = (pending = 0, held = 0) => ({pending, held});
const board = () => ({rows: [
  {chaseId: "case-A", patientLabel: "A", communicationReview: counts(), source: {revision: 2}},
  {chaseId: "case-B", patientLabel: "B", communicationReview: counts(), state: "in_progress"},
], counts: {open: 2}, policy: {approved: false}});

test("only validated badge counts change; source, status, order and board metadata are retained", () => {
  const current = board(), saved = structuredClone(current);
  const incoming = {rows: [{chaseId: "new-case", communicationReview: counts(99)},
    {chaseId: "case-B", communicationReview: counts(2, 1), state: "verified_closed"},
    {chaseId: "case-A", communicationReview: counts(1), patientLabel: "replacement", source: {revision: 999}}]};
  const merged = mergeListedCounts(current, readListedCounts(incoming, listedCaseIds(current)));
  assert.deepEqual(current, saved);
  assert.deepEqual(merged.rows.map((r) => r.chaseId), ["case-A", "case-B"]);
  assert.equal(merged.rows[0].patientLabel, "A");
  assert.equal(merged.rows[0].source, current.rows[0].source);
  assert.equal(merged.rows[1].state, "in_progress");
  assert.equal(merged.policy, current.policy); assert.equal(merged.counts, current.counts);
  assert.deepEqual(merged.rows.map((r) => r.communicationReview), [counts(1), counts(2, 1)]);
});

test("unavailable counts, missing cases, duplicate identities and malformed numbers cannot become zero", () => {
  assert.equal(hasCommunicationCounts({rows: [{chaseId: "case-A"}]}), false);
  assert.equal(hasCommunicationCounts({rows: []}), false);
  assert.equal(hasCommunicationCounts(board()), true);
  for (const value of [counts(true), counts(-1), counts(1, 2), counts(Number.MAX_SAFE_INTEGER + 1), null]) {
    assert.throws(() => readListedCounts({rows: [{chaseId: "case-A", communicationReview: value}]}, ["case-A"]));
  }
  assert.throws(() => readListedCounts({rows: []}, ["case-A"]));
  assert.throws(() => readListedCounts({rows: [{chaseId: "case-A"}, {chaseId: "case-A"}]}, ["case-A"]));
  assert.throws(() => readListedCounts(board(), ["case-A", "case-A"]));
  assert.throws(() => listedCaseIds({rows: [{chaseId: "../other-practice"}]}));
});

test("a newer selected-case count does not discard another case's incoming update", () => {
  const current = board(); current.rows[0].communicationReview = counts(3, 1);
  const earlier = new Map([["case-A", counts()], ["case-B", counts(2)]]);
  const merged = mergeListedCounts(current, earlier, new Set(["case-A"]));
  assert.equal(merged.rows[0].communicationReview, current.rows[0].communicationReview);
  assert.deepEqual(merged.rows[1].communicationReview, counts(2));
  assert.throws(() => mergeListedCounts(current, earlier, new Set(["unknown-case"])));
});

function harness() {
  let next = 1, tick, visible, busy = false;
  const deadlines = new Map(), signals = [], requests = [], accepted = [], failures = [];
  const page = {hidden: false, addEventListener: (_name, fn) => { visible = fn; },
    removeEventListener: (_name, fn) => { assert.equal(fn, visible); visible = null; }};
  const timer = {setInterval: (fn, ms) => { assert.equal(ms, 15_000); tick = fn; return 1; },
    clearInterval: () => { tick = null; }, setTimeout: (fn, ms) => { assert.equal(ms, 10_000); const id = next++; deadlines.set(id, fn); return id; },
    clearTimeout: (id) => deadlines.delete(id)};
  const stop = pollVisibleCounts({page, timer, blocked: () => busy,
    read: (signal) => { signals.push(signal); return new Promise((resolve, reject) => requests.push({resolve, reject})); },
    accept: (v) => accepted.push(v), failed: (e) => failures.push(e.message)});
  return {page, requests, signals, accepted, failures, stop,
    tick: () => tick?.(), visibility: (hidden) => { page.hidden = hidden; visible?.(); },
    block: (value) => { busy = value; }, expire: () => [...deadlines.values()].forEach((fn) => fn()), deadlines};
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("polls are bounded, skip hidden/blocked/overlapping reads, and resume on visibility", async () => {
  const h = harness();
  h.block(true); h.tick(); assert.equal(h.requests.length, 0);
  h.block(false); h.visibility(true); h.tick(); assert.equal(h.requests.length, 0);
  h.visibility(false); assert.equal(h.requests.length, 1);
  h.tick(); assert.equal(h.requests.length, 1);
  h.requests[0].resolve("current counts"); await settle();
  assert.deepEqual(h.accepted, ["current counts"]);
  h.tick(); assert.equal(h.requests.length, 2);
  h.visibility(true); assert.equal(h.signals[1].aborted, true);
  h.requests[1].resolve("old hidden result"); await settle();
  assert.deepEqual(h.accepted, ["current counts"]);
  h.visibility(false); assert.equal(h.requests.length, 3);
  h.stop(); h.requests[2].resolve("after unmount"); await settle();
  assert.deepEqual(h.accepted, ["current counts"]);
  assert.equal(h.signals[2].aborted, true); assert.equal(h.deadlines.size, 0);
});

test("timeout/failure reports stale without accepting data; a subsequent bounded refresh recovers", async () => {
  const h = harness(); h.tick(); h.expire();
  assert.equal(h.signals[0].aborted, true);
  h.requests[0].resolve("late response ignoring abort"); await settle();
  assert.equal(h.accepted.length, 0); assert.equal(h.failures.length, 1);
  h.tick(); h.requests[1].reject(new Error("failed count read")); await settle();
  assert.equal(h.failures.length, 2);
  h.tick(); h.requests[2].resolve("recovered counts"); await settle();
  assert.deepEqual(h.accepted, ["recovered counts"]); h.stop();
});
