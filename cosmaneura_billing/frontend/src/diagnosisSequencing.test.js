import test from "node:test";
import assert from "node:assert/strict";
import golden from "./operationalSuggestedCodes.golden.json" with { type: "json" };
import { primaryBadge, sequencingMessage, validDiagnosisSequencing } from "./diagnosisSequencing.js";
import { visibleSuggestedCodes } from "./operationalDemoContract.js";
import { preparedBillingBlock } from "./providerBilling.js";

function summary(status = "unresolved_explicit") {
  return {
    rule_version: "diagnosis-sequencing.2026-09-30.v1", status,
    selection_basis: status === "resolved_explicit" ? "explicit_extracted"
      : status === "legacy_fallback" ? "legacy_first_captured" : "none",
    requires_review: status !== "resolved_explicit",
    message: "The primary diagnosis remains unresolved; confirm sequencing in human review.",
  };
}

test("sequencing distinguishes captured intent, legacy order and unresolved/conflicting intent", () => {
  for (const status of ["resolved_explicit", "legacy_fallback", "unresolved_explicit", "conflicting_explicit", "not_established"]) {
    const value = summary(status);
    assert.equal(validDiagnosisSequencing(value), true);
    assert.equal(sequencingMessage(value), value.message);
    assert.equal(primaryBadge(value), status === "resolved_explicit" ? "primary candidate"
      : status === "legacy_fallback" ? "provisional order" : null);
  }
  assert.equal(primaryBadge(undefined), "primary candidate");
  assert.match(sequencingMessage(undefined), /not reported/);
});

test("malformed sequencing cannot manufacture a primary badge or bypass the dormant contract", () => {
  for (const value of [null, {}, {...summary(), rule_version: "unknown"},
    {...summary(), status: "constructor"}, {...summary(), requires_review: false},
    {...summary(), selection_basis: "explicit_extracted"}, {...summary(), injected: true},
    {...summary(), message: "x".repeat(401)}, {...summary(), message: "bad\u0000"},
    {...summary(), message: "bad\ud800"}]) {
    assert.equal(validDiagnosisSequencing(value), false);
    assert.equal(primaryBadge(value), null);
    assert.match(sequencingMessage(value), /could not be verified/);
    assert.deepEqual(visibleSuggestedCodes({...golden.payload, diagnosis_sequencing: value}, golden.documents), []);
  }
});

test("valid secondaries remain copyable with unresolved sequencing and legacy responses remain readable", () => {
  const payload = {...golden.payload, diagnosis_sequencing: summary()};
  assert.deepEqual(visibleSuggestedCodes(payload, golden.documents).map((r) => r.code),
    golden.payload.codes.map((r) => r.code));
  delete payload.diagnosis_sequencing;
  assert.equal(visibleSuggestedCodes(payload, golden.documents).length, golden.payload.codes.length);
});

test("prepared reference retains valid secondary codes and the unresolved sequencing warning", () => {
  const encounter = {encounterId: "SYN-SEQUENCE", encounterVersion: 1, signedNote: "Cough assessed."};
  const value = summary();
  const result = {encounterVersion: 1, diagnosis_sequencing: value, clinical_note_modified: false,
    cpt_candidates: [], icd_candidates: [{code: "R05.8", status: "suggested",
      evidence_spans: [{source: "signed_note", text: encounter.signedNote, loc: {start: 0, end: 15}}]}]};
  const prepared = preparedBillingBlock(result, encounter);
  assert.match(prepared, /ICD-10-CM suggestions: R05.8/);
  assert.ok(prepared.includes(value.message));
  assert.equal(result.diagnosis_sequencing, value);
});

test("backend golden includes a bounded explicit primary state without changing captured code evidence", () => {
  assert.equal(validDiagnosisSequencing(golden.payload.diagnosis_sequencing), true);
  assert.equal(golden.payload.diagnosis_sequencing.status, "resolved_explicit");
  assert.deepEqual(golden.payload.codes.filter((r) => r.primary).map((r) => r.code), ["K62.5"]);
});
