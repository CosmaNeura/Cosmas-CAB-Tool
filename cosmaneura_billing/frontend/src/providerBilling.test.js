import assert from "node:assert/strict";
import test from "node:test";

import {
  completenessLabel,
  evidenceSupportedEm,
  isCurrentEncounterResult,
  isLocatedEvidenceSpan,
  isSyntheticDemoEncounter,
  isValidEncounterVersion,
  preparedBillingBlock,
  sourceLabel,
  suggestedCandidates,
  visitTypeLabel,
} from "./providerBilling.js";

const signedNote = "Assessment: acute cough.";
const evidence = [{ source: "signed_note", text: signedNote, loc: { start: 0, end: signedNote.length } }];
const encounter = {
  encounterId: "SYN-RC-0003",
  encounterVersion: 4,
  signedNote,
  transcript: "",
  intake: "",
};
const result = {
  encounterVersion: 4,
  dataClass: "synthetic_demo_release_candidate",
  engine_version: "rules-v1",
  inferenceMode: "mock",
  inferenceModel: "deterministic-fixture",
  clinical_note_modified: false,
  em: {
    provider_selected_level: "99213", suggested_level: "99214",
    status: "suggested", evidence_spans: evidence,
  },
  completeness: { rulepack: "telehealth.v1", complete: false, results: [
    { status: "present", provider_override: { overridden: false } },
    { status: "missing", provider_override: { overridden: false } },
  ] },
  cpt_candidates: [
    { code: "99214", status: "suggested", evidence_spans: evidence },
    { code: "99215", status: "held", evidence_spans: evidence },
  ],
  icd_candidates: [
    { code: "R05.1", status: "suggested", evidence_spans: evidence },
    { code: "J18.9", status: "removed", evidence_spans: [] },
  ],
};

test("formats server-owned provenance and visit types without inferring from IDs", () => {
  assert.equal(sourceLabel("local_synthetic_demo"), "Local-only synthetic demo fixture");
  assert.equal(sourceLabel("synthetic_demo_release_candidate"), "Validated synthetic release-candidate fixture");
  assert.equal(sourceLabel("unrecognized"), "Server-verified source");
  assert.equal(visitTypeLabel("telehealth_established"), "Telehealth Established");
  assert.equal(isSyntheticDemoEncounter({
    id: "not-a-special-prefix", readOnly: true,
    dataClass: "synthetic_demo_release_candidate",
  }), true);
  assert.equal(isSyntheticDemoEncounter({
    id: "ENC-100237", readOnly: true,
    dataClass: "local_synthetic_demo",
  }), true);
  assert.equal(isSyntheticDemoEncounter({
    id: "ENC-100237", readOnly: false,
    dataClass: "local_synthetic_demo",
  }), false);
  assert.equal(isSyntheticDemoEncounter({
    id: "SYN-RC-0003", readOnly: false, dataClass: "operational",
  }), false);
});

test("completeness distinguishes blockers, completed checks, and uncovered visits", () => {
  assert.equal(completenessLabel(null), "Not checked");
  assert.equal(completenessLabel({ rulepack: null, complete: null, results: [] }), "No rule pack");
  assert.equal(completenessLabel({ rulepack: "telehealth.v1", complete: true, results: [] }), "All requirements found");
  assert.equal(completenessLabel(result.completeness), "1 blocker");
  assert.equal(completenessLabel({
    rulepack: "telehealth.v1", complete: true,
    results: [{ status: "missing", provider_override: { overridden: true } }],
  }), "Cleared with 1 reasoned override");
});

test("copy selection includes only exactly located evidence-backed suggestions", () => {
  assert.deepEqual(suggestedCandidates(result, encounter).map((candidate) => candidate.code), ["99214", "R05.1"]);
  assert.deepEqual(suggestedCandidates({ ...result, degraded: { reason: "offline" } }, encounter), []);
  assert.equal(evidenceSupportedEm(result, encounter)?.code, "99214");
  assert.equal(evidenceSupportedEm({
    ...result,
    em: { ...result.em, status: "held", evidence_spans: [] },
  }, encounter), null);
  assert.deepEqual(suggestedCandidates({
    ...result,
    cpt_candidates: [{ code: "99214", status: "suggested", evidence_spans: [{ text: "   " }] }],
    icd_candidates: [],
  }, encounter), []);
});

test("evidence must be an exact bounded slice of a known loaded source", () => {
  assert.equal(isLocatedEvidenceSpan(evidence[0], encounter), true);
  assert.equal(isLocatedEvidenceSpan({ ...evidence[0], text: "acute cough" }, encounter), false);
  assert.equal(isLocatedEvidenceSpan({ ...evidence[0], loc: null }, encounter), false);
  assert.equal(isLocatedEvidenceSpan({ ...evidence[0], loc: { start: 0, end: 200 } }, encounter), false);
  assert.equal(isLocatedEvidenceSpan({ ...evidence[0], source: "unknown" }, encounter), false);
  assert.equal(isLocatedEvidenceSpan(
    { source: "transcript", text: "cough", loc: { start: 2, end: 7 } },
    { ...encounter, transcript: "🙂 cough" },
  ), true);
  const tampered = {
    ...result,
    cpt_candidates: [{ code: "99214", status: "suggested", evidence_spans: [
      { ...evidence[0], loc: { start: 1, end: signedNote.length } },
    ] }],
    icd_candidates: [],
  };
  assert.deepEqual(suggestedCandidates(tampered, encounter), []);
});

test("billing results must match the exact positive encounter version", () => {
  assert.equal(isValidEncounterVersion(4), true);
  assert.equal(isValidEncounterVersion(0), false);
  assert.equal(isValidEncounterVersion("4"), false);
  assert.equal(isCurrentEncounterResult(result, encounter), true);
  assert.equal(isCurrentEncounterResult({ ...result, encounterVersion: 5 }, encounter), false);
  assert.equal(isCurrentEncounterResult(result, { ...encounter, encounterVersion: null }), false);
});

test("prepared block is human-controlled, evidence-filtered, and explicitly not transmitted", () => {
  const text = preparedBillingBlock(result, encounter);
  assert.match(text, /STATUS: PREPARED FOR HUMAN REVIEW — NOT TRANSMITTED/);
  assert.match(text, /CPT \/ E\/M suggestions: 99214/);
  assert.match(text, /ICD-10-CM suggestions: R05\.1/);
  assert.match(text, /Encounter version: 4/);
  assert.doesNotMatch(text, /99215|J18\.9/);
  assert.match(text, /Clinical note modified by CAB: No/);
  assert.match(text, /Nothing was sent to Athena, an EHR, a clearinghouse, or a payer/);
});

test("no prepared block is produced for a degraded result", () => {
  assert.equal(preparedBillingBlock({ ...result, degraded: { reason: "offline" } }, encounter), "");
});

test("no prepared block is produced across an encounter version mismatch", () => {
  assert.equal(preparedBillingBlock({ ...result, encounterVersion: 5 }, encounter), "");
  assert.equal(preparedBillingBlock(result, { ...encounter, encounterVersion: null }), "");
});

test("prepared block never labels a held evidence-free E/M as supported", () => {
  const held = {
    ...result,
    em: { ...result.em, status: "held", evidence_spans: [] },
    cpt_candidates: [],
  };
  assert.match(
    preparedBillingBlock(held, encounter),
    /Evidence-supported E\/M: None suggested/,
  );
});
