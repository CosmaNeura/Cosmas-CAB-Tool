import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dataReviewText, dataSectionSummary, readDataReview } from "./mdmDataReview.js";

const golden = JSON.parse(readFileSync(new URL("../../tests/fixtures/mdm_data_review.json", import.meta.url)));
for (const revision of ["2026-09-30.v1", "2026-09-30.v2", "2026-10-02.v3"]) test(`original ${revision} data decisions remain readable and exportable`, () => {
  const review = {...structuredClone(golden.display), rule_version: `office-mdm-data.${revision}`};
  const before = structuredClone(review);
  assert.equal(readDataReview(review, golden.encounter), review);
  assert.match(dataReviewText(review), new RegExp(`office-mdm-data.${revision}`));
  assert.deepEqual(review, before);
});
test("additive golden ledger preserves counted, duplicate and held evidence in Unicode coordinates", () => {
  const before = structuredClone(golden);
  assert.equal(readDataReview(golden.display, golden.encounter), golden.display);
  assert.deepEqual(golden, before);
  assert.match(dataReviewText(golden.display), /duplicate: Test result review — CBC/);
  assert.match(dataReviewText(golden.display), /signed_note, characters 2–14: CBC ordered\./);
  assert.match(dataReviewText(golden.display), /held: Unclassified data — Home log/);
  assert.equal(readDataReview(undefined, golden.encounter), null);
});

for (const [name, tamper] of [
  ["wrong quote", (r) => {r.events[0].evidence_spans[0].text = "invented";}],
  ["UTF16 offsets", (r) => {r.events[0].evidence_spans[0].loc.start = 3;}],
  ["missing counted evidence", (r) => {r.events[0].evidence_spans = [];}],
  ["duplicate event ID", (r) => {r.events[1].id = r.events[0].id;}],
  ["invented units", (r) => {r.category_one_units = 2;}],
  ["invented historian", (r) => {r.independent_historian = true;}],
  ["unknown version", (r) => {r.rule_version = "future";}],
  ["array action", (r) => {r.events[0].action = ["test_order"];}],
  ["source changed", (r) => {r.events[0].evidence_spans[0].source = "transcript";}],
]) test(`rejects ${name}`, () => {
  const review = structuredClone(golden.display); tamper(review);
  assert.throws(() => readDataReview(review, golden.encounter), /invalid-data-review/);
});

test("incomplete display holds the affected event and never publishes aggregate counts", () => {
  const review = structuredClone(golden.display);
  Object.assign(review.events[0], {status: "held", evidence_spans: [], evidence_complete: false});
  review.evidence_complete = false;
  assert.equal(readDataReview(review, golden.encounter), review);
  assert.match(dataReviewText(review), /summary unavailable/);
  assert.doesNotMatch(dataReviewText(review), /category-one units 1/);
});

test("considered tests retain their reported action without implying an order", () => {
  const review = structuredClone(golden.display);
  review.events[0].action = "test_considered";
  assert.equal(readDataReview(review, golden.encounter), review);
  assert.match(dataReviewText(review), /counted: Test considered — CBC/);
});

test("element summary withholds a classification when ledger evidence is incomplete", () => {
  const complete = readDataReview(structuredClone(golden.display), golden.encounter);
  assert.equal(dataSectionSummary(complete), `Element level: ${complete.level}.`);
  const incomplete = {...complete, evidence_complete: false};
  assert.match(dataSectionSummary(incomplete), /Summary unavailable/);
  assert.doesNotMatch(dataSectionSummary(incomplete), /Element level:/);
  assert.equal(dataSectionSummary({...complete, events: []}), "No qualifying data activity was established.");
});
