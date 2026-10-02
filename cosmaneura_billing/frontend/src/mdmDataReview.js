import { isLocatedEvidenceSpan } from "./providerBilling.js";

export const dataReviewVersion = "office-mdm-data.2026-09-30.v1";
const dataReviewVersions = [dataReviewVersion, "office-mdm-data.2026-09-30.v2", "office-mdm-data.2026-10-02.v3"];
export const dataActionLabels = Object.freeze({test_order: "Test order", test_considered: "Test considered", test_result_review: "Test result review",
  external_note_review: "External note review", independent_historian: "Independent historian",
  independent_interpretation: "Independent interpretation", external_discussion: "External discussion",
  unclassified: "Unclassified data"});
const categories = ["category_one", "historian", "interpretation", "discussion"];
const text = (value, max) => typeof value === "string" && Array.from(value).length <= max
  && value.trim().length > 0 && !/[\u0000-\u0008\u000b-\u001f\u007f\ud800-\udfff]/u.test(value);
function require(ok) { if (!ok) throw new Error("invalid-data-review"); }
const keys = (value, expected) => value && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).sort().join("|") === [...expected].sort().join("|");

// This verifies display provenance and internal counts; it never computes an E/M level.
export function readDataReview(review, encounter) {
  if (review == null) return null;
  require(keys(review, ["rule_version", "level", "category_one_units", "independent_historian",
    "independent_interpretation", "external_discussion", "qualifying_categories", "events", "evidence_complete"]));
  require(dataReviewVersions.includes(review.rule_version)
    && ["straightforward", "low", "moderate", "high"].includes(review.level)
    && Number.isInteger(review.category_one_units) && review.category_one_units >= 0 && review.category_one_units <= 41
    && [review.independent_historian, review.independent_interpretation, review.external_discussion,
      review.evidence_complete].every((v) => typeof v === "boolean")
    && Array.isArray(review.qualifying_categories) && review.qualifying_categories.length <= 3
    && review.qualifying_categories.every((c) => ["category_one", "interpretation", "discussion"].includes(c))
    && new Set(review.qualifying_categories).size === review.qualifying_categories.length
    && Array.isArray(review.events) && review.events.length <= 40);
  const ids = new Set(), counted = [];
  for (const event of review.events) {
    require(keys(event, ["id", "action", "subject", "status", "category", "reason", "evidence_spans", "evidence_complete"])
      && text(event.id, 32) && !ids.has(event.id) && typeof event.action === "string" && Object.hasOwn(dataActionLabels, event.action)
      && text(event.subject, 160) && text(event.reason, 300)
      && ["counted", "duplicate", "held"].includes(event.status) && [...categories, null].includes(event.category)
      && typeof event.evidence_complete === "boolean" && Array.isArray(event.evidence_spans)
      && event.evidence_spans.length <= 3 && (!event.evidence_complete || event.evidence_spans.length > 0));
    ids.add(event.id);
    for (const span of event.evidence_spans) require(keys(span, ["source", "text", "loc"])
      && keys(span.loc, ["start", "end"]) && text(span.text, 1000) && isLocatedEvidenceSpan(span, encounter));
    if (event.status === "counted") {
      require(event.evidence_complete && event.category !== null
        && event.evidence_spans.every((span) => span.source !== "intake_text"));
      counted.push(event.category);
    }
  }
  if (review.evidence_complete) require(
    review.category_one_units === counted.filter((c) => c === "category_one").length + Number(counted.includes("historian"))
    && review.independent_historian === counted.includes("historian")
    && review.independent_interpretation === counted.includes("interpretation")
    && review.external_discussion === counted.includes("discussion"));
  return review;
}

export function dataSectionSummary(review) {
  if (!review.evidence_complete) return "Summary unavailable: retained source evidence is incomplete.";
  if (!review.events.some((event) => event.status === "counted")) return "No qualifying data activity was established.";
  return `Element level: ${review.level}.`;
}

export function dataReviewText(review) {
  const summary = review.evidence_complete
    ? `MDM data: ${review.level}; category-one units ${review.category_one_units} (includes a qualifying independent historian, when present).`
    : "MDM data summary unavailable: source evidence or displayed counts are incomplete.";
  return [summary, `Rule: ${review.rule_version}`, ...review.events.flatMap((event) => [
    `${event.status}: ${dataActionLabels[event.action]} — ${event.subject}. ${event.reason}`,
    ...event.evidence_spans.map((span) => `${span.source}, characters ${span.loc.start}–${span.loc.end}: ${span.text}`),
    ...(!event.evidence_complete ? ["Complete source evidence was not located."] : []),
  ])].join("\n");
}
