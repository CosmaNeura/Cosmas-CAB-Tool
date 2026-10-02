import test from "node:test";
import assert from "node:assert/strict";

import {
  buildFeedbackPayload, canPrepareCoderPreview, coderErrorMessage, coderQueueState,
  initialCoderDraft, reviewableCoderView, validateCoderDraft,
} from "./coderReview.js";

const engine = {
  encounterId: "SYN-1",
  encounterVersion: 3,
  reviewContext: "opaque-server-context",
  submittedEm: "99213",
  em: { kind: "EM", code: "99214", why: "Moderate MDM.", evidence: "Moderate MDM evidence.", evidenceSpans: [{ source: "signed_note", text: "Moderate MDM evidence." }] },
  dx: [{ id: "r051", kind: "ICD10", code: "R05.1", why: "Acute cough.", evidence: "Acute cough documented.", evidenceSpans: [{ source: "signed_note", text: "Acute cough documented." }] }],
  withheld: [{ id: "z800", kind: "ICD10", code: "Z80.0", status: "rejected", why: "Contradicted.", evidence: "No family history.", evidenceSpans: [{ source: "transcript", text: "No family history." }] }],
  completeness: { complete: true, results: [] },
};

test("every engine recommendation begins explicitly unreviewed", () => {
  const draft = initialCoderDraft(engine);
  assert.deepEqual(Object.values(draft.decisions).map((item) => item.action), ["unreviewed", "unreviewed"]);
  assert.equal(validateCoderDraft(engine, draft.decisions, []).valid, false);
});

test("evidence-free codes are removed from the actionable coder view", () => {
  const reviewable = reviewableCoderView({
    ...engine,
    em: { kind: "EM", code: "99214", why: "No evidence.", evidence: "", evidenceSpans: [] },
    dx: [...engine.dx, { id: "bad", kind: "PROCEDURE", code: "J18.9", why: "Wrong kind.", evidence: "Wrong-kind evidence.", evidenceSpans: [{ source: "signed_note", text: "Wrong-kind evidence." }] }],
    withheld: [...engine.withheld, { id: "bad2", kind: "ICD10", code: "E66.8", status: "held", why: "No support.", evidence: "", evidenceSpans: [] }],
  });
  assert.equal(reviewable.em, null);
  assert.deepEqual(reviewable.dx.map((row) => row.code), ["R05.1"]);
  assert.deepEqual(reviewable.withheld.map((row) => row.code), ["Z80.0"]);
  assert.equal(reviewable.contractViolations, 3);
});

test("a reasoned rejected exclusion without evidence is suppressed, not made actionable", () => {
  const reviewable = reviewableCoderView({
    ...engine,
    withheld: [{ ...engine.withheld[0], evidence: "", evidenceSpans: [] }],
  });
  assert.deepEqual(reviewable.withheld, []);
  assert.equal(reviewable.suppressedExclusions, 1);
  assert.equal(reviewable.contractViolations, 0);
});

test("a degraded no-code response suppresses an evidence-free held fallback", () => {
  const reviewable = reviewableCoderView({
    ...engine,
    em: null,
    dx: [],
    degraded: { reason: "offline_model_unsupported_encounter" },
    withheld: [{
      id: "99212", kind: "EM", code: "99212", status: "held",
      why: "Fallback level was held.", evidence: "", evidenceSpans: [],
    }],
  });
  assert.deepEqual(reviewable.withheld, []);
  assert.equal(reviewable.suppressedExclusions, 1);
  assert.equal(reviewable.contractViolations, 0);
});

test("edits, rejections, and human additions require rationale and evidence", () => {
  const draft = initialCoderDraft(engine);
  draft.decisions.em = { action: "modify", finalCode: "99215", reasonCode: "", rationale: "" };
  draft.decisions["dx:0"] = { action: "reject", finalCode: "", reasonCode: "", rationale: "" };
  const result = validateCoderDraft(engine, draft.decisions, [
    { id: "manual", kind: "ICD10", code: "J18.9", rationale: "", evidence: "" },
  ]);
  assert.equal(result.valid, false);
  assert.match(result.issues.map((item) => item.message).join(" "), /edit reason/);
  assert.match(result.issues.map((item) => item.message).join(" "), /why R05.1 is rejected/);
  assert.match(result.issues.map((item) => item.message).join(" "), /supporting documentation evidence/);
});

test("a complete human review builds the exact feedback contract without transmission fields", () => {
  const decisions = {
    em: { action: "modify", finalCode: "99215", reasonCode: "em_doc_supports_higher_level", rationale: "Additional moderate-risk decision documented." },
    "dx:0": { action: "accept", finalCode: "R05.1", reasonCode: "", rationale: "" },
  };
  const additions = [{ id: "manual", kind: "ICD10", code: "J18.9", rationale: "Assessment names pneumonia.", evidence: "Pneumonia assessed and treated." }];
  assert.equal(validateCoderDraft(engine, decisions, additions).valid, true);
  const payload = buildFeedbackPayload({ engine, decisions, additions, reviewId: "review-1", seconds: 12.9 });
  assert.equal(payload.review_id, "review-1");
  assert.equal(payload.encounter_version, 3);
  assert.equal(payload.final_em, "99215");
  assert.equal(payload.time_spent_seconds, 12);
  assert.equal(payload.decisions.length, 3);
  assert.match(payload.decisions.at(-1).note, /Rationale: Assessment names pneumonia/);
  assert.match(payload.decisions.at(-1).note, /Evidence: Pneumonia assessed and treated/);
  assert.equal("transmit" in payload, false);
  assert.equal("claim" in payload, false);
});

test("uncovered and incomplete visits allow explicit feedback without a prepared state", () => {
  const decisions = {
    em: { action: "accept" },
    "dx:0": { action: "accept" },
  };
  for (const complete of [null, false]) {
    const view = reviewableCoderView({
      ...engine,
      completeness: { complete, rulepack: complete === null ? null : "telehealth.v1", results: [] },
    });
    assert.equal(view.contractViolations, 0);
    assert.equal(validateCoderDraft(view, initialCoderDraft(view).decisions, []).valid, false);
    assert.equal(validateCoderDraft(view, decisions, []).valid, true);
    assert.equal(canPrepareCoderPreview(view), false);
    assert.equal(
      coderQueueState({ engine: view, draftValid: true, saved: true }),
      "Reviewed — completeness unresolved",
    );
    assert.equal(
      coderQueueState({ engine: view, draftValid: true, saved: false }),
      complete === false ? "Completeness blocked" : "Completeness not checked",
    );
    const payload = buildFeedbackPayload({ engine: view, decisions, additions: [], reviewId: "review-unresolved", seconds: 1 });
    assert.equal(payload.decisions.length, 2);
    assert.equal("completeness" in payload, false);
    assert.equal("prepared" in payload, false);
  }
  assert.equal(canPrepareCoderPreview(engine), true);
  assert.equal(canPrepareCoderPreview({ ...engine, degraded: {} }), false);
});

test("missing and malformed completeness responses still reject feedback and preparation", () => {
  for (const completeness of [undefined, {}, { complete: "true", results: [] }, { complete: true }]) {
    const malformed = reviewableCoderView({ ...engine, completeness });
    assert.equal(malformed.completenessContractValid, false);
    assert.ok(malformed.contractViolations > 0);
    const result = validateCoderDraft(malformed, { em: { action: "accept" }, "dx:0": { action: "accept" } }, []);
    assert.equal(result.valid, false);
    assert.ok(result.issues.some((item) => item.key === "completeness"));
    assert.equal(canPrepareCoderPreview(malformed), false);
  }
});

test("reconsidering an excluded code preserves its engine disposition", () => {
  const decisions = {
    em: { action: "accept", finalCode: "99214", reasonCode: "", rationale: "" },
    "dx:0": { action: "accept", finalCode: "R05.1", reasonCode: "", rationale: "" },
  };
  const additions = [{
    id: "manual-held", sourceDisposition: "rejected", kind: "ICD10",
    code: "Z80.0", rationale: "Coder independently supports it.",
    evidence: "Family history documented in the signed note.",
  }];
  const payload = buildFeedbackPayload({
    engine, decisions, additions, reviewId: "review-held", seconds: 4,
  });
  const added = payload.decisions.at(-1);
  assert.equal(added.reason_code, "other");
  assert.match(added.note, /Original engine disposition: rejected/);
});

test("queue labels distinguish clean, clarification, and reviewed states", () => {
  assert.equal(coderQueueState({ engine, draftValid: false, saved: false }), "Needs review");
  assert.equal(coderQueueState({ engine, draftValid: true, saved: false }), "Clean");
  assert.equal(coderQueueState({ engine: { ...engine, degraded: {} }, draftValid: false, saved: false }), "Awaiting provider clarification");
  assert.equal(coderQueueState({ engine, draftValid: true, saved: true }), "Reviewed/prepared");
});

test("coder errors are fixed, non-reflective, and keep retry/transmission semantics clear", () => {
  assert.match(coderErrorMessage({ status: 409, message: "sensitive" }, "save"), /frozen draft remains visible/);
  assert.doesNotMatch(coderErrorMessage({ status: 409, message: "sensitive" }, "save"), /sensitive/);
  assert.match(coderErrorMessage(new Error("secret"), "load"), /No browser fallback/);
  assert.match(coderErrorMessage(new Error("secret"), "save"), /exact frozen payload can be retried/);
  assert.match(coderErrorMessage(new Error("secret"), "verify"), /prior review receipt remains confirmed/);
});

test("serialized note, decision-count, code-length, and timer boundaries fail closed", () => {
  const draft = initialCoderDraft(engine);
  draft.decisions.em = { action: "modify", finalCode: "99215", reasonCode: "other", rationale: "x".repeat(4_000) };
  draft.decisions["dx:0"] = { action: "accept", finalCode: "R05.1", reasonCode: "", rationale: "" };
  const tooMany = Array.from({ length: 499 }, (_, index) => ({
    id: `manual-${index}`, kind: "ICD10", code: "X".repeat(index === 0 ? 33 : 3),
    rationale: "Reason", evidence: "Evidence",
  }));
  const result = validateCoderDraft(engine, draft.decisions, tooMany);
  assert.ok(result.issues.some((item) => /500-decision/.test(item.message)));
  assert.ok(result.issues.some((item) => /Shorten the rationale/.test(item.message)));
  assert.ok(result.issues.some((item) => /32 characters/.test(item.message)));

  const validDecisions = {
    em: { action: "accept", finalCode: "99214", reasonCode: "", rationale: "" },
    "dx:0": { action: "accept", finalCode: "R05.1", reasonCode: "", rationale: "" },
  };
  const payload = buildFeedbackPayload({ engine, decisions: validDecisions, additions: [], reviewId: "review-2", seconds: 99_999 });
  assert.equal(payload.time_spent_seconds, 86_400);
});
