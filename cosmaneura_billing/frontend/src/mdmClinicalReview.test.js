import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { clinicalReviewText, clinicalSectionSummary, readClinicalReview } from "./mdmClinicalReview.js";

const golden = JSON.parse(readFileSync(new URL("../../tests/fixtures/mdm_clinical_review.json", import.meta.url)));
for (const [revision, riskRevision] of [["v1", "v1"], ["v2", "v2"], ["v3", "v3"], ["v4", "v3"], ["v4", "v4"]]) test(`original problems ${revision} / risk ${riskRevision} clinical decisions remain readable and exportable`, () => {
  const review = structuredClone(golden.display);
  review.problems.rule_version = `office-mdm-problems.2026-09-30.${revision}`;
  review.risk.rule_version = `office-mdm-risk.2026-09-30.${riskRevision}`;
  const before = structuredClone(review);
  assert.equal(readClinicalReview(review, golden.encounter), review);
  assert.match(clinicalReviewText(review, golden.encounter), new RegExp(`office-mdm-problems.2026-09-30.${revision}`));
  assert.deepEqual(review, before);
});
test("clinical display golden preserves original decisions and exact Unicode coordinates", () => {
  const before = structuredClone(golden);
  assert.equal(readClinicalReview(golden.display, golden.encounter), golden.display);
  const exported = clinicalReviewText(golden.display, golden.encounter);
  assert.match(exported, /Problems addressed: Element level: low/);
  assert.match(exported, /Management risk: Element level: moderate/);
  assert.match(exported, /duplicate: HTN/);
  assert.match(exported, /held: Historical rash/);
  assert.match(exported, /signed_note, characters 2–47: Hypertension/);
  assert.deepEqual(golden, before);
  assert.equal(readClinicalReview(undefined, golden.encounter), null);
});

for (const [name, mutate] of [
  ["wrong quote", (r) => {r.problems.events[0].evidence_spans[0].text = "invented";}],
  ["UTF16 offsets", (r) => {r.problems.events[0].evidence_spans[0].loc.start = 3;}],
  ["boolean offsets", (r) => {r.problems.events[0].evidence_spans[0].loc.start = true;}],
  ["unretained counted evidence", (r) => {r.problems.events[0].evidence_complete = false;}],
  ["empty counted evidence", (r) => {r.risk.events[0].evidence_spans = [];}],
  ["wrong source", (r) => {r.risk.events[0].evidence_spans[0].source = "transcript";}],
  ["duplicate event ID", (r) => {r.problems.events[1].id = r.problems.events[0].id;}],
  ["wrong section category", (r) => {r.problems.events[0].category = "prescription_management";}],
  ["array category", (r) => {r.risk.events[0].category = ["prescription_management"];}],
  ["future version", (r) => {r.problems.rule_version = "future";}],
  ["unpublished risk version", (r) => {r.risk.rule_version = "office-mdm-risk.2026-09-30.v5";}],
  ["unsupported aggregate", (r) => {r.problems.events = []; r.problems.level = "high";}],
  ["extra top-level field", (r) => {r.selectedLevel = "high";}],
  ["too many events", (r) => {r.risk.events = Array(21).fill(r.risk.events[0]);}],
  ["too many anchors", (r) => {r.risk.events[0].evidence_spans = Array(4).fill(r.risk.events[0].evidence_spans[0]);}],
  ["duplicate anchor", (r) => {r.risk.events[0].evidence_spans.push(r.risk.events[0].evidence_spans[0]);}],
  ["invalid subject", (r) => {r.risk.events[0].subject = "bad\ud800";}],
]) test(`clinical reader and export reject ${name}`, () => {
  const review = structuredClone(golden.display); mutate(review);
  assert.throws(() => readClinicalReview(review, golden.encounter), /invalid-clinical-review/);
  assert.throws(() => clinicalReviewText(review, golden.encounter), /invalid-clinical-review/);
});

test("partial retained evidence hides only its section aggregate and never assigns a replacement", () => {
  const review = structuredClone(golden.display);
  Object.assign(review.problems.events[0], {status: "held", evidence_spans: [], evidence_complete: false});
  review.problems.evidence_complete = false;
  assert.equal(readClinicalReview(review, golden.encounter), review);
  const exported = clinicalReviewText(review, golden.encounter);
  assert.match(exported, /Problems addressed: Summary unavailable/);
  assert.doesNotMatch(exported, /Element level: low/);
  assert.match(exported, /Management risk: Element level: moderate/);
  assert.equal(review.problems.level, "low");
});

test("held-only and empty elements do not claim an established minimal level", () => {
  const review = structuredClone(golden.display);
  review.problems.events = [review.problems.events[2]];
  review.problems.level = "straightforward";
  review.risk.events = []; review.risk.level = "straightforward";
  assert.equal(readClinicalReview(review, golden.encounter), review);
  const exported = clinicalReviewText(review, golden.encounter);
  assert.match(exported, /No qualifying classification was established/);
  assert.doesNotMatch(exported, /Element level/);
  assert.match(clinicalSectionSummary(review.risk), /No qualifying classification/);
});

test("ambiguous repeated text cannot acquire an invented unique location", () => {
  const encounter = {...golden.encounter, signedNote: golden.encounter.signedNote + "\nHistorical rash is listed."};
  assert.throws(() => readClinicalReview(golden.display, encounter), /invalid-clinical-review/);
});
