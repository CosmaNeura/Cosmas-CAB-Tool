import assert from "node:assert/strict";
import test from "node:test";

import {
  completenessLabel,
  evidenceSupportedEm,
  isSyntheticDemoEncounter,
  preparedBillingBlock,
  sourceLabel,
  suggestedCandidates,
  visitTypeLabel,
} from "./providerBilling.js";

const evidence = [{ source: "signed_note", text: "Assessment: acute cough.", loc: { start: 12, end: 36 } }];
const result = {
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
  assert.equal(sourceLabel("synthetic_demo_release_candidate"), "Validated synthetic release-candidate fixture");
  assert.equal(sourceLabel("unrecognized"), "Server-verified source");
  assert.equal(visitTypeLabel("telehealth_established"), "Telehealth Established");
  assert.equal(isSyntheticDemoEncounter({
    id: "not-a-special-prefix", readOnly: true,
    dataClass: "synthetic_demo_release_candidate",
  }), true);
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

test("copy selection includes only evidence-backed suggested candidates", () => {
  assert.deepEqual(suggestedCandidates(result).map((candidate) => candidate.code), ["99214", "R05.1"]);
  assert.deepEqual(suggestedCandidates({ ...result, degraded: { reason: "offline" } }), []);
  assert.equal(evidenceSupportedEm(result)?.code, "99214");
  assert.equal(evidenceSupportedEm({
    ...result,
    em: { ...result.em, status: "held", evidence_spans: [] },
  }), null);
  assert.deepEqual(suggestedCandidates({
    ...result,
    cpt_candidates: [{ code: "99214", status: "suggested", evidence_spans: [{ text: "   " }] }],
    icd_candidates: [],
  }), []);
});

test("prepared block is human-controlled, evidence-filtered, and explicitly not transmitted", () => {
  const text = preparedBillingBlock(result, { encounterId: "SYN-RC-0003" });
  assert.match(text, /STATUS: PREPARED FOR HUMAN REVIEW — NOT TRANSMITTED/);
  assert.match(text, /CPT \/ E\/M suggestions: 99214/);
  assert.match(text, /ICD-10-CM suggestions: R05\.1/);
  assert.doesNotMatch(text, /99215|J18\.9/);
  assert.match(text, /Clinical note modified by CAB: No/);
  assert.match(text, /Nothing was sent to Athena, an EHR, a clearinghouse, or a payer/);
});

test("no prepared block is produced for a degraded result", () => {
  assert.equal(preparedBillingBlock({ ...result, degraded: { reason: "offline" } }, { encounterId: "X" }), "");
});

test("prepared block never labels a held evidence-free E/M as supported", () => {
  const held = {
    ...result,
    em: { ...result.em, status: "held", evidence_spans: [] },
    cpt_candidates: [],
  };
  assert.match(
    preparedBillingBlock(held, { encounterId: "SYN-RC-0003" }),
    /Evidence-supported E\/M: None suggested/,
  );
});
