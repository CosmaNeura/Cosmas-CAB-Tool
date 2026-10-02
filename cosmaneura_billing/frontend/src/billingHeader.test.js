import test from "node:test";
import assert from "node:assert/strict";
import { factKeys, fieldAuthor, headerDraft, readBillingHeader } from "./billingHeader.js";

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
