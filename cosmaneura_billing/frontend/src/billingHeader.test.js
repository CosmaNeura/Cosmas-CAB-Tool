import test from "node:test";
import assert from "node:assert/strict";
import { beginHeaderRequest, cancelHeaderRequest, exportBillingHeader, factKeys, fieldAuthor, finishHeaderRequest, headerDraft, headerProposals, ownsHeaderRequest,
  headerReviewRequest, headerSourceKey, missingHeaderBaseline, readBillingHeader } from "./billingHeader.js";

function fixture() {
  const actor = {id: "11111111-1111-4111-8111-111111111111", name: "Synthetic provider", role: "provider"};
  const pin = {encounterId: "synthetic", encounterVersion: 1, noteId: "note", noteVersion: "1",
    noteSha256: "a".repeat(64), supportingSourceDigests: [], bundleSha256: "b".repeat(64)};
  const encounter = {encounterId: "synthetic", encounterVersion: 1, headerSource: pin,
    signedNote: "😀 café documented", transcript: "", intake: ""};
  const header = {schemaVersion: "provider-billing-header.v1", headerId: "22222222-2222-4222-8222-222222222222",
    expectedSource: structuredClone(pin), reviewStatus: "unreviewed", createdBy: actor, generatedAt: "2026-09-29T12:00:00Z",
    facts: factKeys.map((key) => ({key, label: key, status: "not_documented", value: null, evidenceIds: []})),
    evidence: [{id: "e1", source: "note", sourceId: "note", sourceVersion: "1", start: 2, end: 7,
      text: "café", coordinateSystem: "unicode_code_point"}],
    suggestions: [{id: "s1", kind: "EM", code: "99213", label: "Office E/M", evidenceIds: ["e1"], reviewStatus: "unreviewed", basis: "Synthetic example"}],
    flags: [], policy: {status: "not_verified", message: "Human review"},
    generation: {inferenceMode: "mock", runId: actor.id}};
  return {encounter, view: {header, revision: null, revisionNumber: 0}, actor};
}

test("exact Unicode code-point evidence and JSONB key reordering remain valid", () => {
  const {encounter, view} = fixture();
  view.header.expectedSource = Object.fromEntries(Object.entries(view.header.expectedSource).reverse());
  assert.equal(readBillingHeader(view, encounter), view);
});

for (const [label, tamper] of [
  ["wrong note version", (v) => {v.header.expectedSource.noteVersion = "2";}],
  ["UTF16 offset", (v) => {v.header.evidence[0].start = 3;}],
  ["unanchored quote", (v) => {v.header.evidence[0].text = "fabricated";}],
  ["missing evidence", (v) => {v.header.suggestions[0].evidenceIds = [];}],
  ["unknown reference", (v) => {v.header.suggestions[0].evidenceIds = ["elsewhere"];}],
  ["duplicate facts", (v) => {v.header.facts[1].key = v.header.facts[0].key;}],
  ["unknown schema", (v) => {v.header.schemaVersion = "future";}],
]) {
  test(`rejects entire response for ${label}`, () => {
    const {encounter, view} = fixture(); tamper(view);
    assert.throws(() => readBillingHeader(view, encounter));
  });
}

test("draft is detached and fact attribution follows the actual last editor", () => {
  const {view, actor} = fixture();
  const draft = headerDraft(view);
  draft.codes[0].code = "99214";
  assert.equal(view.header.suggestions[0].code, "99213");
  view.revision = {history: [
    {actor, changes: [{path: "facts.patient_location"}]},
    {actor: {...actor, role: "coder"}, changes: [{path: "note"}]},
  ]};
  assert.equal(fieldAuthor(view, "facts.patient_location").role, "provider");
  assert.equal(fieldAuthor(view, "codes.s1"), undefined);
});

function signed(view, actor) {
  view.signoff = {id: actor.id, reviewId: actor.id, headerId: view.header.headerId, headerRevision: 0,
    expectedSource: structuredClone(view.header.expectedSource), actor: {...actor, role: "coder"},
    createdAt: "2026-09-29T13:00:00Z", status: "recorded", recommendationDigest: "c".repeat(64),
    humanProposalDecisions: [], reviewerNote: "Reviewed synthetic note",
    codeDecisions: [{kind: "EM", engine_code: "99213", action: "accept", final_code: "99213", note: ""}]};
  return view;
}

test("only a source-bound current-revision coder receipt can render", () => {
  const {view, encounter, actor} = fixture(); signed(view, actor);
  assert.equal(readBillingHeader(view, encounter), view);
  for (const mutate of [
    (r) => {r.headerRevision = 1;},
    (r) => {r.expectedSource.noteVersion = "old";},
    (r) => {r.actor.role = "provider";},
    (r) => {r.headerId = actor.id;},
    (r) => {r.recommendationDigest = "invalid";},
    (r) => {r.codeDecisions[0].final_code = null;},
    (r) => {r.codeDecisions[0].final_code = "99214";},
    (r) => {r.codeDecisions[0].action = "add";},
  ]) {
    const changed = structuredClone(view); mutate(changed.signoff);
    assert.throws(() => readBillingHeader(changed, encounter));
  }
});

test("provenance-only source changes remount the header", () => {
  const {encounter} = fixture();
  assert.notEqual(headerSourceKey(encounter), headerSourceKey({...encounter,
    headerSource: {...encounter.headerSource, ingestionSource: "different-source"}}));
});

test("human proposal and removal decisions must cover the exact displayed revision", () => {
  const {view, actor} = fixture();
  const draft = headerDraft(view);
  draft.codes = [{id: "added", kind: "ICD10", code: "R51.9", label: "Human proposal", evidenceIds: ["e1"]}];
  view.revision = {draft, history: [{actor, changes: [{path: "codes.s1"}, {path: "codes.added"}]}]};
  view.revisionNumber = 1;
  assert.deepEqual(headerProposals(view).map((p) => [p.id, p.original?.code, p.proposed?.code]),
    [["s1", "99213", undefined], ["added", undefined, "R51.9"]]);
  const feedback = {encounter_id: "synthetic", encounter_version: 1,
    decisions: [{kind: "EM", engine_code: "99213", action: "reject", final_code: null}]};
  const decisions = [{id: "s1", decision: "accept", reason: "Removed after review"},
    {id: "added", decision: "accept", reason: "Reviewed original evidence"}];
  assert.throws(() => headerReviewRequest(view, feedback, decisions, false));
  assert.throws(() => headerReviewRequest(view, feedback, decisions.slice(1), true));
  assert.throws(() => headerReviewRequest(view, {...feedback, encounter_id: "other"}, decisions, true));
  const payload = headerReviewRequest(view, feedback, decisions, true);
  assert.equal(payload.expectedRevision, 1);
  payload.humanProposalDecisions[0].reason = "changed";
  assert.equal(decisions[0].reason, "Removed after review");
});

test("plain-text export retains evidence, source pin and recorded decisions", () => {
  const {view, actor} = fixture();
  assert.match(exportBillingHeader(view), /Coder review pending/);
  signed(view, actor);
  const exported = exportBillingHeader(view);
  assert.match(exported, /Coder review recorded/);
  assert.match(exported, /Recommendation digest: c{64}/);
  assert.match(exported, /EM 99213: accept → 99213/);
  assert.match(exported, /\[e1\] note, note, version 1, 2–7\ncafé/);
  assert.match(exported, /clinical note unchanged; no claim submitted/);
});

test("same-source drift needs explicit consistent baseline decisions without changing originals", () => {
  const {view, encounter, actor} = fixture();
  const feedback = {encounter_id: "synthetic", encounter_version: 1,
    decisions: [{kind: "EM", engine_code: "99212", action: "accept", final_code: "99212", note: ""}]};
  const before = structuredClone(view);
  assert.deepEqual(missingHeaderBaseline(view, feedback.decisions).map((r) => r.code), ["99213"]);
  assert.throws(() => headerReviewRequest(view, feedback, [], true));
  const choice = {id: "s1", decision: "reject", reason: "Reviewed the earlier E/M"};
  const payload = headerReviewRequest(view, feedback, [], true, [choice]);
  assert.deepEqual(view, before);
  assert.equal(payload.feedback.decisions.length, 1);
  signed(view, actor);
  view.signoff.codeDecisions = feedback.decisions;
  assert.throws(() => readBillingHeader(view, encounter)); // legacy receipt cannot hide omitted baseline
  view.signoff.baselineDecisions = [choice];
  assert.equal(readBillingHeader(view, encounter), view);
  assert.match(exportBillingHeader(view), /Earlier suggestion EM 99213: reject/);
  for (const choices of [[], [choice, choice], [{...choice, id: "other"}], [{...choice, reason: " "}],
    [{...choice, decision: "retain"}]]) {
    const bad = structuredClone(view); bad.signoff.baselineDecisions = choices;
    assert.throws(() => readBillingHeader(bad, encounter));
  }
  view.signoff.baselineDecisions[0].decision = "retain";
  view.signoff.codeDecisions[0] = {kind: "EM", engine_code: "99212", action: "modify",
    final_code: "99213", note: "Reviewed current source"};
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(headerReviewRequest({...view, signoff: null}, {...feedback,
    decisions: view.signoff.codeDecisions}, [], true, view.signoff.baselineDecisions).baselineDecisions,
  view.signoff.baselineDecisions);
});


test("overlapping header refreshes replace reads without stale completion or cleanup unlocking the new owner", async () => {
  for (const staleFinishesFirst of [true, false]) {
    const pending = {current: null}, state = {busy: false, value: null};
    function start(value) {
      const request = beginHeaderRequest(pending, null, true);
      assert.ok(request);
      state.busy = true;
      let resolve;
      const result = new Promise((done) => {resolve = done;});
      const completion = (async () => {
        try {
          await result;
          if (ownsHeaderRequest(pending, request)) state.value = value;
        } finally {
          if (finishHeaderRequest(pending, request)) state.busy = false;
        }
      })();
      return {request, resolve, completion, cleanup: () => cancelHeaderRequest(pending, request)};
    }
    const old = start("old receipt");
    old.cleanup(); // refreshKey cleanup before replacement effect
    const latest = start("latest receipt");
    assert.equal(old.request.controller.signal.aborted, true);
    old.cleanup(); // a late cleanup must not abort or unlock the replacement
    assert.equal(latest.request.controller.signal.aborted, false);
    if (staleFinishesFirst) {
      old.resolve(); await old.completion;
      assert.deepEqual(state, {busy: true, value: null});
      assert.equal(pending.current, latest.request);
    }
    latest.resolve(); await latest.completion;
    old.resolve(); await old.completion;
    assert.deepEqual(state, {busy: false, value: "latest receipt"});
    assert.equal(pending.current, null);
  }
});

test("refresh preserves an in-flight mutation and its exact retry command", () => {
  const pending = {current: null};
  const command = {path: "/api/encounters/header/save", body: {operationId: "same-request", expectedRevision: 2}};
  const mutation = beginHeaderRequest(pending, command);
  assert.equal(beginHeaderRequest(pending, null, true), null);
  assert.equal(mutation.controller.signal.aborted, false);
  assert.equal(pending.current, mutation);
  assert.equal(finishHeaderRequest(pending, mutation), true);
  const retry = beginHeaderRequest(pending, command);
  assert.equal(retry.command, command);
  cancelHeaderRequest(pending, mutation);
  assert.equal(pending.current, retry);
  cancelHeaderRequest(pending); // unmount/source-key change cancels the actual owner
  assert.equal(retry.controller.signal.aborted, true);
  assert.equal(pending.current, null);
});


test("an ambiguous completed save blocks late refreshes and new writes until its exact retry resolves", () => {
  const pending = {current: null};
  const command = {path: "/api/encounters/header/save", body: {operationId: "original-save", expectedRevision: 2}};
  const save = beginHeaderRequest(pending, command);
  const unresolvedCommand = command; // A 503 cannot determine whether the save committed.
  assert.equal(finishHeaderRequest(pending, save), true);
  assert.equal(pending.current, null);
  assert.equal(beginHeaderRequest(pending, null, true, unresolvedCommand), null);
  assert.equal(beginHeaderRequest(pending, null, false, unresolvedCommand), null);
  assert.equal(beginHeaderRequest(pending, {...command}, false, unresolvedCommand), null);
  const retry = beginHeaderRequest(pending, command, false, unresolvedCommand);
  assert.equal(retry.command, command);
  assert.equal(retry.command.body.operationId, "original-save");
  assert.equal(beginHeaderRequest(pending, null, true, unresolvedCommand), null);
  assert.equal(retry.controller.signal.aborted, false);
  assert.equal(finishHeaderRequest(pending, retry), true);
  // Only a resolved retry clears the held command; ordinary reads can then resume.
  const refreshed = beginHeaderRequest(pending, null, true, null);
  assert.ok(refreshed);
  cancelHeaderRequest(pending);
});
