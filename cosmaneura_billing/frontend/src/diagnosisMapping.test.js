import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {mappingAddition, mappingCandidates, mappingProposal, mappingReviewText, readCatalog, validMapping} from "./diagnosisMapping.js";
import {acceptedDiagnosisOrder, diagnosisOrderDecisionId, exportBillingHeader, factKeys, headerDraft, headerProposals, readBillingHeader, replaceHeaderCodes} from "./billingHeader.js";

const golden = JSON.parse(readFileSync(new URL("./diagnosisMapping.golden.json", import.meta.url)));
function fixture() {
  const actor = {id: "11111111-1111-4111-8111-111111111111", role: "provider", name: "Synthetic provider"};
  const note = golden.label + ".";
  const pin = {encounterId: "synthetic", encounterVersion: 1, noteId: "note", noteVersion: "1", noteSha256: "b".repeat(64), supportingSourceDigests: [], bundleSha256: "c".repeat(64)};
  const encounter = {encounterId: "synthetic", encounterVersion: 1, headerSource: pin, signedNote: note, transcript: "", intake: ""};
  const header = {schemaVersion: "provider-billing-header.v1", headerId: "22222222-2222-4222-8222-222222222222", reviewStatus: "unreviewed", expectedSource: pin, createdBy: actor, generatedAt: "2026-09-30T12:00:00Z",
    facts: factKeys.map((key) => ({key, label: key, status: "not_documented", value: null, evidenceIds: []})),
    evidence: [{id: "e1", source: "note", sourceId: "note", sourceVersion: "1", start: 0, end: note.length, text: note, coordinateSystem: "unicode_code_point"}], suggestions: [],
    flags: [{id: "diagnosis-review-0", message: "Migraine concept — held for human mapping.", evidenceIds: ["e1"]}],
    policy: {status: "not_verified", message: "Human review required"}, generation: {inferenceMode: "mock", runId: actor.id}};
  return {actor, encounter, view: {header, revision: null, revisionNumber: 0}};
}
function saved() {
  const f = fixture(), draft = headerDraft(f.view);
  draft.codes = [structuredClone(golden)]; draft.diagnosisOrderConfirmed = true;
  f.view.revisionNumber = 1;
  f.view.revision = {number: 1, headerId: f.view.header.headerId, expectedSource: f.view.header.expectedSource, reviewStatus: "unreviewed", actor: f.actor, createdAt: "2026-09-30T12:05:00Z", reason: "Attributes reviewed", draft,
    history: [{actor: f.actor, createdAt: "2026-09-30T12:05:00Z", reason: "Attributes reviewed", changes: [{path: "codes.human-mapping", label: "Human code proposal", before: null, after: structuredClone(golden)}]}]};
  return f;
}

test("catalog selection is explicit and matches the bounded mapping golden", () => {
  const {view} = fixture(), candidate = mappingCandidates(view.header)[0];
  assert.deepEqual(headerDraft(view).codes, []);
  assert.deepEqual(mappingProposal(candidate, golden, golden.mapping.catalogVersion, golden.id), golden);
  const result = {headerId: view.header.headerId, revision: 0, expectedSource: view.header.expectedSource,
    candidateId: candidate.id, catalogVersion: golden.mapping.catalogVersion, matches: [{code: golden.code, label: golden.label, description: golden.label}], nextOffset: null};
  assert.equal(readCatalog(result, view, candidate), result);
  assert.equal(readCatalog({...result, nextOffset: 1020}, view, candidate).nextOffset, 1020);
  for (const patch of [{revision: 1}, {expectedSource: {}}, {candidateId: "other"}, {catalogVersion: "unverified"}, {nextOffset: 1_000_001}, {matches: [{code: "G43", label: "invented", description: "not this label"}]}]) {
    assert.throws(() => readCatalog({...result, ...patch}, view, candidate));
  }
});

test("mapping metadata and original evidence survive save read, human ordering and export", () => {
  const {view, encounter} = saved();
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(headerProposals(view)[0].proposed, golden);
  assert.throws(() => exportBillingHeader(view));
  const output = exportBillingHeader(view, encounter);
  assert.match(output, /Human mapping of diagnosis-review-0/);
  assert.match(output, /Human mapping proposed; coder review pending/);
  assert.match(output, /Coder-confirmed primary: unresolved/);
  assert.match(output, /Migraine concept — held for human mapping/);
  assert.deepEqual(acceptedDiagnosisOrder(view), []);
  const changed = replaceHeaderCodes(headerDraft(view), [{...golden, mapping: {...golden.mapping, catalogVersion: "icd10cm-sha256:" + "d".repeat(64)}}]);
  assert.equal(changed.diagnosisOrderConfirmed, false);
});

test("mapping cannot borrow another flag/evidence or attach to an engine row", () => {
  const {view, encounter} = saved();
  for (const mutate of [(r) => {r.mapping.candidateId = "other";}, (r) => {r.mapping.catalogVersion = "unknown";},
    (r) => {r.mapping.accepted = true;}, (r) => {r.evidenceIds = [];}, (r) => {r.kind = "EM";}]) {
    const invalid = structuredClone(view); mutate(invalid.revision.draft.codes[0]);
    assert.throws(() => readBillingHeader(invalid, encounter));
  }
  view.header.suggestions.push({...golden, reviewStatus: "unreviewed", basis: "Synthetic"});
  assert.equal(validMapping(golden, view.header), false);
});

test("explicit mapping-to-coder staging keeps rationale blank and source exact", () => {
  const {view} = saved();
  const addition = mappingAddition(view, golden, "separate-human-addition");
  assert.equal(addition.rationale, ""); assert.equal(addition.evidence, view.header.evidence[0].text);
  assert.equal(addition.kind, "ICD10"); assert.equal(addition.code, golden.code);
  assert.deepEqual(view.header.suggestions, []);
});

test("only accepted human code plus mapping and order decisions resolve human primary", () => {
  const {view, encounter, actor} = saved();
  view.signoff = {id: actor.id, reviewId: actor.id, headerId: view.header.headerId, headerRevision: 1, expectedSource: view.header.expectedSource,
    actor: {...actor, role: "coder"}, status: "recorded", createdAt: "2026-09-30T12:10:00Z", recommendationDigest: "d".repeat(64), reviewerNote: "Source reviewed",
    humanProposalDecisions: [{id: golden.id, decision: "accept", reason: "Mapping reviewed"}, {id: diagnosisOrderDecisionId, decision: "accept", reason: "Order reviewed"}],
    codeDecisions: [{kind: "ICD10", engine_code: null, action: "add", final_code: golden.code, note: "Explicit human addition"}]};
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(acceptedDiagnosisOrder(view).map((r) => r.code), [golden.code]);
  assert.match(exportBillingHeader(view, encounter), /Coder-confirmed primary: G43.009/);
  assert.equal(mappingReviewText(view, golden), "Coder accepted this human mapping.");
  view.signoff.humanProposalDecisions[0].decision = "reject";
  assert.throws(() => readBillingHeader(view, encounter));
  view.signoff.humanProposalDecisions[1].decision = "reject";
  assert.throws(() => readBillingHeader(view, encounter)); // Rejecting order does not excuse contradictory mapping outcome.
  view.signoff.codeDecisions = [];
  assert.equal(readBillingHeader(view, encounter), view);
  assert.deepEqual(acceptedDiagnosisOrder(view), []);
  assert.equal(mappingReviewText(view, golden), "Coder rejected this human mapping.");
});
