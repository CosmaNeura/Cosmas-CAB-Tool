import { readFileSync } from "node:fs";
import test from "node:test";
import { armHeaderRequestDeadline, HEADER_REQUEST_TIMEOUT_MS, HEADER_GENERATION_TIMEOUT_MS, readHeaderGeneration } from "./billingHeader.js";
import assert from "node:assert/strict";
import { beginHeaderRequest, cancelHeaderRequest, exportBillingHeader, factKeys, fieldAuthor, finishHeaderRequest, headerDraft, headerProposals, ownsHeaderRequest, headerReviewRequest, headerSourceKey, missingHeaderBaseline, readBillingHeader, acceptedDiagnosisOrder, diagnosisOrderDecisionId, diagnosisOrderReviewText, headerDecisionIds, moveHeaderDiagnosis, proposedDiagnosisOrder, replaceHeaderCodes,  } from "./billingHeader.js";

test("request deadline releases only its owner and preserves the exact retry command", (t) => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  const pending = {current: null}, command = {path: "/api/encounters/header/save", body: {operationId: "same-operation"}};
  let unresolved = null;
  const expired = beginHeaderRequest(pending, command);
  armHeaderRequestDeadline(pending, expired, () => { unresolved = command; });
  t.mock.timers.tick(HEADER_REQUEST_TIMEOUT_MS - 1);
  assert.equal(expired.controller.signal.aborted, false);
  t.mock.timers.tick(1);
  assert.equal(expired.controller.signal.aborted, true);
  assert.equal(pending.current, null);
  assert.equal(unresolved, command);
  assert.equal(beginHeaderRequest(pending, {...command}, false, unresolved), null);
  const retry = beginHeaderRequest(pending, command, false, unresolved);
  assert.equal(retry.command, command);
  assert.equal(ownsHeaderRequest(pending, expired), false);
  assert.equal(finishHeaderRequest(pending, expired), false); // Late result cannot release the retry.
  assert.equal(pending.current, retry);
  cancelHeaderRequest(pending);
});

test("completion and unmount or supersession cancellation clear owned deadlines", (t) => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  const pending = {current: null};
  let timeouts = 0;
  const done = beginHeaderRequest(pending);
  armHeaderRequestDeadline(pending, done, () => timeouts++);
  finishHeaderRequest(pending, done);
  const replaced = beginHeaderRequest(pending);
  armHeaderRequestDeadline(pending, replaced, () => timeouts++);
  const current = beginHeaderRequest(pending, null, true);
  armHeaderRequestDeadline(pending, current, () => timeouts++);
  cancelHeaderRequest(pending, replaced); // Stale cleanup cannot cancel the current owner.
  assert.equal(current.controller.signal.aborted, false);
  cancelHeaderRequest(pending);
  assert.equal(current.controller.signal.aborted, true);
  t.mock.timers.tick(HEADER_GENERATION_TIMEOUT_MS);
  assert.equal(timeouts, 0);
});

test("generation gets its longer finite budget instead of an ordinary read deadline", (t) => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  const pending = {current: null};
  let timeouts = 0;
  const request = beginHeaderRequest(pending, {path: "/api/encounters/header/generate", body: {operationId: "generation"}});
  armHeaderRequestDeadline(pending, request, () => timeouts++);
  t.mock.timers.tick(HEADER_REQUEST_TIMEOUT_MS);
  assert.equal(timeouts, 0);
  assert.equal(ownsHeaderRequest(pending, request), true);
  t.mock.timers.tick(HEADER_GENERATION_TIMEOUT_MS - HEADER_REQUEST_TIMEOUT_MS);
  assert.equal(timeouts, 1);
  assert.equal(request.controller.signal.aborted, true);
  assert.equal(pending.current, null);
});

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

test("held-concept review flags retain exact evidence without a code proposal", () => {
  const {encounter, view} = fixture();
  view.header.suggestions = [];
  view.header.flags = [{id: "held-concept", message: "Unresolved clinical concept — coding review required.", evidenceIds: ["e1"]}];
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(headerDraft(view).codes, []);
  const exported = exportBillingHeader(view);
  assert.ok(exported.includes("Unresolved clinical concept — coding review required. [e1]"));
  assert.ok(exported.includes("café"));
  for (const refs of [[], ["unknown"], ["e1", "e1"], null]) {
    const invalid = structuredClone(view); invalid.header.flags[0].evidenceIds = refs;
    assert.throws(() => readBillingHeader(invalid, encounter));
  }
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
test("unresolved primary notice retains exact evidence through header read and export", () => {
  const {encounter, view} = fixture();
  view.header.flags.push({id: "primary_diagnosis_sequencing", evidenceIds: ["e1"],
    message: "The extracted primary diagnosis candidate is unresolved or held. Confirm sequencing in review."});
  assert.equal(readBillingHeader(view, encounter), view);
  const draft = headerDraft(view);
  draft.note = "Human review of secondary diagnoses.";
  assert.match(exportBillingHeader(view, encounter), /primary diagnosis candidate is unresolved/);
  assert.match(exportBillingHeader(view, encounter), /café/);
  assert.equal(Object.hasOwn(draft, "flags"), false);
  view.header.flags[0].evidenceIds = ["missing"];
  assert.throws(() => readBillingHeader(view, encounter));
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

function orderedFixture() {
  const f = fixture(), {view, actor} = f;
  view.header.suggestions.push(...[["dx1", "R51.9"], ["dx2", "R53.83"]].map(([id, code]) =>
    ({...view.header.suggestions[0], id, kind: "ICD10", code, label: "Synthetic diagnosis"})));
  view.header.flags.push({id: "primary_diagnosis_sequencing", message: "Original primary unresolved; held concept.", evidenceIds: ["e1"]});
  const draft = headerDraft(view); draft.diagnosisOrderConfirmed = true;
  view.revisionNumber = 1;
  view.revision = {number: 1, headerId: view.header.headerId, expectedSource: view.header.expectedSource,
    reviewStatus: "unreviewed", actor, createdAt: "2026-09-29T13:00:00Z", reason: "Source reviewed", draft,
    history: [{actor, createdAt: "2026-09-29T13:00:00Z", reason: "Source reviewed", changes: [
      {path: "diagnosisOrder", label: "Human diagnosis order", before: false, after: true}]}]};
  signed(view, actor); view.signoff.headerRevision = 1;
  view.signoff.codeDecisions.push(...["R51.9", "R53.83"].map((code) =>
    ({kind: "ICD10", engine_code: code, action: "accept", final_code: code, note: ""})));
  view.signoff.humanProposalDecisions = [{id: diagnosisOrderDecisionId, decision: "accept", reason: "Source verified"}];
  return f;
}

test("explicit saved order survives read and export independently of original held intent", () => {
  const {view, encounter} = orderedFixture(), before = structuredClone(view.header);
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(proposedDiagnosisOrder(view).map((row) => row.code), ["R51.9", "R53.83"]);
  assert.deepEqual(acceptedDiagnosisOrder(view).map((row) => row.code), ["R51.9", "R53.83"]);
  assert.deepEqual(headerProposals(view), []); // unchanged code content still needs order affirmation
  assert.deepEqual(headerDecisionIds(view), [diagnosisOrderDecisionId]);
  const exported = exportBillingHeader(view, encounter);
  assert.match(exported, /Coder-confirmed primary: R51\.9/);
  assert.match(exported, /Coder-confirmed order: R51\.9 → R53\.83/);
  assert.match(exported, /Saved proposal \(revision 1\) by Synthetic provider/);
  assert.match(exported, /Original primary unresolved; held concept\. \[e1\]/);
  assert.match(exported, /R51\.9 \[e1\]/);
  assert.deepEqual(view.header, before);
  assert.throws(() => exportBillingHeader(view)); // exact source needed for new confirmation
  view.header.evidence[0].text = "not the source";
  assert.throws(() => exportBillingHeader(view, encounter));
});

test("rejected primary cannot be replaced by a surviving secondary at read, request or export", () => {
  const {view, encounter} = orderedFixture();
  const primary = view.signoff.codeDecisions.find((row) => row.final_code === "R51.9");
  primary.action = "reject"; primary.final_code = null;
  assert.throws(() => readBillingHeader(view, encounter));
  assert.throws(() => exportBillingHeader(view, encounter));
  const feedback = {encounter_id: "synthetic", encounter_version: 1, decisions: view.signoff.codeDecisions};
  assert.throws(() => headerReviewRequest({...view, signoff: null}, feedback, view.signoff.humanProposalDecisions, true));
  view.signoff.humanProposalDecisions[0].decision = "reject";
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(acceptedDiagnosisOrder(view), []);
  assert.match(exportBillingHeader(view, encounter), /Coder-confirmed primary: unresolved/);
  assert.doesNotMatch(exportBillingHeader(view, encounter), /Coder-confirmed primary: R53\.83/);
});

test("secondary removal retains primary, but final diagnoses outside saved order fail closed", () => {
  const {view, encounter} = orderedFixture();
  const secondary = view.signoff.codeDecisions.find((row) => row.final_code === "R53.83");
  secondary.action = "reject"; secondary.final_code = null;
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(acceptedDiagnosisOrder(view).map((r) => r.code), ["R51.9"]);
  view.signoff.codeDecisions.push({kind: "ICD10", engine_code: null, action: "add", final_code: "Z31.69", note: "Source verified"});
  assert.throws(() => readBillingHeader(view, encounter));
});

test("legacy and new unconfirmed arrays establish no human primary", () => {
  const {view, encounter} = orderedFixture();
  delete view.revision.draft.diagnosisOrderConfirmed;
  view.signoff.humanProposalDecisions = [];
  assert.equal(headerDraft(view).diagnosisOrderConfirmed, false);
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(proposedDiagnosisOrder(view), []);
  assert.match(exportBillingHeader(view, encounter), /Unconfirmed — list order/);
  assert.doesNotMatch(exportBillingHeader(view, encounter), /Coder-confirmed primary: R/);
  for (const flag of [1, 0, null, "true", {}, []]) {
    view.revision.draft.diagnosisOrderConfirmed = flag;
    assert.throws(() => readBillingHeader(view, encounter));
  }
  view.revision.draft.diagnosisOrderConfirmed = false;
  view.signoff.humanProposalDecisions = [{id: diagnosisOrderDecisionId, decision: "accept", reason: "Not legitimate"}];
  assert.throws(() => readBillingHeader(view, encounter));
});

test("editor clears confirmation on ICD membership, code, evidence, kind and order changes", () => {
  const {view} = orderedFixture(), draft = headerDraft(view);
  for (const change of [
    (rows) => rows.filter((row) => row.id !== "dx1"),
    (rows) => [...rows, {...rows[1], id: "new", code: "Z31.69"}],
    (rows) => rows.map((row) => row.id === "dx1" ? {...row, code: "Z31.69"} : row),
    (rows) => rows.map((row) => row.id === "dx1" ? {...row, evidenceIds: []} : row),
    (rows) => rows.map((row) => row.id === "dx1" ? {...row, kind: "EM"} : row),
    (rows) => rows.toReversed(),
  ]) assert.equal(replaceHeaderCodes(draft, change(draft.codes)).diagnosisOrderConfirmed, false);
  const moved = moveHeaderDiagnosis(draft, "dx2", 0);
  assert.equal(moved.diagnosisOrderConfirmed, false);
  assert.deepEqual(moved.codes.map((r) => r.id), ["s1", "dx2", "dx1"]);
  assert.equal(replaceHeaderCodes(draft, draft.codes.map((row) => row.kind === "EM" ? {...row, code: "99214"} : row)).diagnosisOrderConfirmed, true);
  assert.equal(draft.diagnosisOrderConfirmed, true); // original draft is detached
});

test("changed evidence proposal requires acceptance before its row can be human primary", () => {
  const {view, encounter} = orderedFixture();
  view.revision.draft.codes.find((row) => row.id === "dx1").label = "New human description";
  view.signoff.humanProposalDecisions.push({id: "dx1", decision: "reject", reason: "Reject change"});
  assert.throws(() => readBillingHeader(view, encounter));
  view.signoff.humanProposalDecisions[1].decision = "accept";
  assert.equal(readBillingHeader(view, encounter), view);
});

test("new receipt needs separate decision and new revision makes old order acceptance unavailable", () => {
  const {view, encounter} = orderedFixture();
  const original = structuredClone(view.signoff);
  view.signoff.humanProposalDecisions = [];
  assert.throws(() => readBillingHeader(view, encounter));
  view.signoff = original;
  view.revisionNumber = 2; view.revision.number = 2;
  view.revision.history.push({...view.revision.history[0], changes: [{path: "note", label: "Note", before: "", after: "Review again"}]});
  assert.throws(() => readBillingHeader(view, encounter));
  view.signoff = null;
  view.previousSignoff = {reviewId: original.reviewId, headerRevision: 1, actor: original.actor, createdAt: original.createdAt};
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(acceptedDiagnosisOrder(view), []);
  assert.match(exportBillingHeader(view, encounter), /Coder-confirmed primary: unresolved/);
});


test("sequencing copy distinguishes pending, accepted and rejected review with original author", () => {
  const {view, encounter} = orderedFixture();
  assert.equal(diagnosisOrderReviewText(view), "Coder acceptance recorded for this revision.");
  let exported = exportBillingHeader(view, encounter);
  assert.match(exported, /Proposed primary: R51\.9\. Coder acceptance recorded for this revision\./);
  assert.match(exported, /Saved proposal \(revision 1\) by Synthetic provider/);
  assert.doesNotMatch(exported, /requires separate coder acceptance|Awaiting separate coder acceptance/);
  view.signoff.humanProposalDecisions[0].decision = "reject";
  assert.equal(diagnosisOrderReviewText(view), "Coder rejected this proposed primary and order.");
  exported = exportBillingHeader(view, encounter);
  assert.match(exported, /Coder rejected this proposed primary and order\./);
  assert.match(exported, /Saved proposal \(revision 1\) by Synthetic provider/);
  assert.match(exported, /Coder-confirmed primary: unresolved/);
  const signed = structuredClone(view.signoff);
  view.signoff = null;
  assert.equal(diagnosisOrderReviewText(view), "Awaiting separate coder acceptance.");
  assert.match(exportBillingHeader(view, encounter), /Awaiting separate coder acceptance\./);
  view.signoff = {...signed, headerRevision: 0};
  assert.equal(diagnosisOrderReviewText(view), "Awaiting separate coder acceptance.");
  view.signoff = {...signed, expectedSource: {...signed.expectedSource, noteVersion: "other"}};
  assert.equal(diagnosisOrderReviewText(view), "Awaiting separate coder acceptance.");
});

test("optional original data ledger is source-bound, exported and excluded from human drafts", () => {
  const {encounter, view} = fixture();
  const ledger = JSON.parse(readFileSync(new URL("../../tests/fixtures/mdm_data_review.json", import.meta.url))).display;
  for (const event of ledger.events) event.evidence_spans = [{source: "signed_note", text: "café", loc: {start: 2, end: 7}}];
  view.header.dataReview = ledger;
  assert.equal(readBillingHeader(view, encounter), view);
  assert.match(exportBillingHeader(view, encounter), /ORIGINAL PIPELINE DATA REVIEW/);
  assert.match(exportBillingHeader(view, encounter), /characters 2–7: café/);
  assert.equal(Object.hasOwn(headerDraft(view), "dataReview"), false);
  assert.throws(() => exportBillingHeader(view), /invalid-header-response/);
  view.header.suggestions = [];
  view.header.dataReview = {...structuredClone(ledger), category_one_units: 0, events: [structuredClone(ledger.events[2])]};
  assert.equal(readBillingHeader(view, encounter), view);
  assert.match(exportBillingHeader(view, encounter), /held: Unclassified data/);
  assert.doesNotMatch(exportBillingHeader(view, encounter), /EM 99213/);
  view.header.dataReview = ledger;
  ledger.events[0].evidence_spans[0].text = "invented";
  assert.throws(() => readBillingHeader(view, encounter));
  assert.throws(() => exportBillingHeader(view, encounter));
});

test("clinical ledger remains original, requires source at export and cannot enter a draft", () => {
  const {encounter, view} = fixture();
  const ledger = JSON.parse(readFileSync(new URL("../../tests/fixtures/mdm_clinical_review.json", import.meta.url))).display;
  for (const name of ["problems", "risk"]) for (const event of ledger[name].events) {
    event.evidence_spans = [{source: "signed_note", text: "café", loc: {start: 2, end: 7}}];
  }
  view.header.clinicalReview = ledger;
  assert.equal(readBillingHeader(view, encounter), view);
  assert.match(exportBillingHeader(view, encounter), /ORIGINAL PIPELINE CLINICAL REVIEW/);
  assert.equal(Object.hasOwn(headerDraft(view), "clinicalReview"), false);
  assert.throws(() => exportBillingHeader(view), /invalid-header-response/);
  ledger.risk.events[0].evidence_spans[0].text = "invented";
  assert.throws(() => readBillingHeader(view, encounter), /invalid-clinical-review/);
  assert.throws(() => exportBillingHeader(view, encounter), /invalid-clinical-review/);
  for (const value of [null, undefined, {}]) {
    view.header.clinicalReview = value;
    assert.throws(() => readBillingHeader(view, encounter));
  }
});

test("expanded reader retains all 320 exact references and rejects overflow", () => {
  const {encounter, view} = fixture();
  const lines = Array.from({length: 320}, (_, i) => `Synthetic source assertion ${i}.`);
  encounter.signedNote = lines.join("\n");
  let start = 0;
  view.header.evidence = lines.map((line, i) => {
    const row = {...view.header.evidence[0], id: `e${i}`, text: line, start, end: start + line.length};
    start = row.end + 1; return row;
  });
  view.header.suggestions[0].evidenceIds = view.header.evidence.map((e) => e.id);
  assert.equal(readBillingHeader(view, encounter), view);
  assert.equal(headerDraft(view).codes[0].evidenceIds.length, 320);
  assert.match(exportBillingHeader(view), /Synthetic source assertion 319\./);
  view.header.evidence.push({...view.header.evidence[0], id: "overflow"});
  assert.throws(() => readBillingHeader(view, encounter));
});

test("copy attaches current source context without changing immutable header or editable draft", () => {
  const {encounter,view}=fixture();
  encounter.headerSource.ingestionSource="provider_completed_note";
  view.header.expectedSource=structuredClone(encounter.headerSource);
  encounter.sourceVisitContext={schemaVersion:"cab.source-visit-context.v1",expectedSource:structuredClone(encounter.headerSource),
    origin:"provider_manifest",sourceSha256:"c".repeat(64),serviceDate:"2026-09-29",noteStatus:"completed",statusMeaning:"provider_attested"};
  const original=JSON.stringify(view),draft=headerDraft(view);
  const exported=exportBillingHeader(view,encounter);
  assert.match(exported,/Date of service: 2026-09-29/);assert.match(exported,/Completed \(provider-confirmed\)/);
  assert.match(exported,/upstream signature not independently verified/);
  assert.equal(JSON.stringify(view),original);assert.deepEqual(headerDraft(view),draft);
  encounter.sourceVisitContext.expectedSource.encounterVersion=2;
  assert.match(exportBillingHeader(view,encounter),/Date of service: Unavailable from source/);
  assert.doesNotMatch(exportBillingHeader(view,encounter),/Date of service: 2026-09-29/);
  delete encounter.sourceVisitContext;
  assert.match(exportBillingHeader(view,encounter),/Note status: Unavailable from source/);
});

test("optional model-input proof is closed historical metadata, not generation permission", () => {
  const {encounter, view} = fixture();
  encounter.headerSource.ingestionSource = "athena_staff_review_operational";
  view.header.expectedSource = structuredClone(encounter.headerSource);
  encounter.inferencePolicy = {allowed:false, reason:"operational_source_model_not_approved"};
  assert.equal(readBillingHeader(view, encounter), view); // Older saved headers lack the additive proof.
  const proof = {schemaVersion:"cab-model-input-proof.v1", policyId:"33333333-3333-4333-8333-333333333333",
    policySha256:"a".repeat(64), sourceSha256:"b".repeat(64), serviceSha256:"c".repeat(64), expiresAt:"2020-01-01T00:00:00Z"};
  view.header.generation.modelInputProof = proof;
  assert.equal(readBillingHeader(view, encounter), view); // Expired authority still describes past generation.
  assert.deepEqual(encounter.inferencePolicy, {allowed:false, reason:"operational_source_model_not_approved"});
  assert.equal(Object.hasOwn(headerDraft(view), "modelInputProof"), false);
  for (const bad of [null, {}, [], {...proof, policyId:"bad"}, {...proof, policySha256:"not-a-hash"},
    {...proof, sourceSha256:null}, {...proof, serviceSha256:"A".repeat(64)}, {...proof, expiresAt:"2020"},
    {...proof, expiresAt:"invalid"}, {...proof, schemaVersion:"future"}, {...proof, allowed:true}]) {
    view.header.generation.modelInputProof = bad;
    assert.throws(() => readBillingHeader(view, encounter));
  }
  view.header.generation.modelInputProof = proof;
  encounter.headerSource.ingestionSource = "local_synthetic_demo";
  view.header.expectedSource = structuredClone(encounter.headerSource);
  assert.throws(() => readBillingHeader(view, encounter));
});

test("provider Azure proof v2 requires the exact source operation/admission and cannot replace Athena v1", () => {
  const {encounter,view}=fixture();
  encounter.headerSource.ingestionSource="provider_completed_note";
  encounter.inferencePolicy={allowed:false,reason:"operational_source_model_not_approved"};
  view.header.expectedSource=structuredClone(encounter.headerSource);
  assert.equal(readBillingHeader(view,encounter),view); // Old offline provider headers have no model proof.
  view.header.generation.inferenceMode="azure_openai";
  const proof={schemaVersion:"cab-model-input-proof.v2",policyId:"33333333-3333-4333-8333-333333333333",
    policySha256:"a".repeat(64),sourceSha256:"b".repeat(64),serviceSha256:"c".repeat(64),expiresAt:"2020-01-01T00:00:00Z",
    sourceOperationId:"44444444-4444-4444-8444-444444444444",sourceAdmissionSha256:"d".repeat(64)};
  view.header.generation.modelInputProof=proof;
  assert.equal(readBillingHeader(view,encounter),view);
  assert.equal(encounter.inferencePolicy.allowed,false); // Historical proof grants no current model authority.
  assert.equal(Object.hasOwn(headerDraft(view),"modelInputProof"),false);
  for (const mutate of [p=>{delete p.sourceOperationId;},p=>{delete p.sourceAdmissionSha256;},
    p=>{p.sourceOperationId="not-a-uuid";},p=>{p.sourceAdmissionSha256="D".repeat(64);},p=>{p.sourceAdmissionSha256=null;},
    p=>{p.schemaVersion="cab-model-input-proof.v1";},p=>{p.schemaVersion="future";},p=>{p.allowed=true;}]) {
    const bad=structuredClone(proof);mutate(bad);view.header.generation.modelInputProof=bad;
    assert.throws(()=>readBillingHeader(view,encounter));
  }
  view.header.generation.modelInputProof=proof;view.header.generation.inferenceMode="mock";
  assert.throws(()=>readBillingHeader(view,encounter));
  view.header.generation.inferenceMode="azure_openai";
  encounter.headerSource.ingestionSource="athena_staff_review_operational";
  view.header.expectedSource=structuredClone(encounter.headerSource);
  assert.throws(()=>readBillingHeader(view,encounter));
  const {sourceOperationId,sourceAdmissionSha256,...athena}=proof;
  view.header.generation.modelInputProof={...athena,schemaVersion:"cab-model-input-proof.v1"};
  assert.equal(readBillingHeader(view,encounter),view);
  encounter.headerSource.ingestionSource="provider_completed_note";
  view.header.expectedSource=structuredClone(encounter.headerSource);
  assert.throws(()=>readBillingHeader(view,encounter));
});


test("async acceptance and status use ordinary deadlines and retain ownership", (t) => {
  t.mock.timers.enable({apis:["setTimeout"]});
  for (const path of ["generate", "generation-status"]) {
    const pending = {current:null}, command = {path:"/api/encounters/header/" + path,
      body:{operationId:"same", ...(path === "generate" ? {responseMode:"async"} : {})}};
    const request = beginHeaderRequest(pending, command);
    let expired = false;
    armHeaderRequestDeadline(pending, request, () => {expired=true});
    t.mock.timers.tick(HEADER_REQUEST_TIMEOUT_MS);
    assert.equal(expired,true);
    assert.equal(ownsHeaderRequest(pending,request),false);
  }
});
test("running and unknown do not parse a fabricated header, and status is exactly pinned", () => {
  const command = {body:{operationId:"same"}};
  for (const status of ["running", "unknown", "failed"]) {
    const response={generation:{operationId:"same",status},view:null,created:false};
    assert.equal(readHeaderGeneration(response, command).status,status);
    assert.throws(() => readHeaderGeneration({...response,created:true},command));
    assert.throws(() => readHeaderGeneration({...response,generation:{operationId:"other",status}},command));
    assert.throws(() => readHeaderGeneration({...response,view:{}},command));
  }
});
