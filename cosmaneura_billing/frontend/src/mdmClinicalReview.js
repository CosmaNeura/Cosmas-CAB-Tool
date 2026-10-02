import { isLocatedEvidenceSpan } from "./providerBilling.js";

export const clinicalReviewVersion = "office-mdm-clinical.2026-09-30.v1";
const sectionVersions = Object.freeze({
  problems: ["office-mdm-problems.2026-09-30.v1", "office-mdm-problems.2026-09-30.v2", "office-mdm-problems.2026-09-30.v3", "office-mdm-problems.2026-09-30.v4"],
  risk: ["office-mdm-risk.2026-09-30.v1", "office-mdm-risk.2026-09-30.v2", "office-mdm-risk.2026-09-30.v3", "office-mdm-risk.2026-09-30.v4"],
});
export const clinicalSections = Object.freeze({problems: "Problems addressed", risk: "Management risk"});
export const clinicalCategoryLabels = Object.freeze({
  self_limited: "Self-limited or minor problem", acute_uncomplicated: "Acute uncomplicated illness or injury",
  stable_acute: "Stable acute illness", stable_chronic: "Stable chronic illness",
  chronic_exacerbation: "Chronic exacerbation, progression or treatment effects",
  undiagnosed_uncertain_prognosis: "Undiagnosed problem with uncertain prognosis",
  acute_systemic: "Acute illness with systemic symptoms", complicated_injury: "Complicated injury",
  severe_chronic: "Severe chronic exacerbation, progression or treatment effects",
  threat_to_life_or_function: "Threat to life or bodily function",
  intensive_toxicity_monitoring: "Intensive toxicity monitoring", patient_specific_management: "Patient-specific management risk",
  parenteral_controlled_medication: "Parenteral controlled medication", prescription_management: "Prescription management",
  nonprescription_management: "Nonprescription management", socially_limited_care: "Care significantly limited by a social condition",
  minimal_management: "Minimal management risk", conservative_management: "Conservative management",
  emergency_major_surgery: "Emergency major surgery", minor_surgery_with_risks: "Minor surgery with identified risks",
  minor_surgery: "Minor surgery", elective_major_surgery_with_risks: "Elective major surgery with identified risks",
  elective_major_surgery: "Elective major surgery", hospitalization: "Hospitalization or escalation decision",
  life_sustaining_deescalation: "Life-sustaining treatment de-escalation",
});
const problemCategories = Object.keys(clinicalCategoryLabels).slice(0, 10);
const categories = {problems: problemCategories, risk: Object.keys(clinicalCategoryLabels).slice(10)};
const keys = (v, expected) => v && !Array.isArray(v) && typeof v === "object"
  && Object.keys(v).length === expected.length && expected.every((k) => Object.hasOwn(v, k));
const text = (v, max) => typeof v === "string" && [...v].length <= max && v.trim().length > 0
  && !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(v)
  && ![...v].some((c) => c.length === 1 && c.charCodeAt(0) >= 0xd800 && c.charCodeAt(0) <= 0xdfff);
const require = (ok) => { if (!ok) throw new Error("invalid-clinical-review"); };

export function readClinicalReview(review, encounter) {
  if (review === undefined) return null;
  require(keys(review, ["rule_version", "problems", "risk"]) && review.rule_version === clinicalReviewVersion);
  for (const name of Object.keys(clinicalSections)) {
    const section = review[name];
    require(keys(section, ["rule_version", "level", "events", "evidence_complete"])
      && sectionVersions[name].includes(section.rule_version)
      && ["straightforward", "low", "moderate", "high"].includes(section.level)
      && typeof section.evidence_complete === "boolean" && Array.isArray(section.events) && section.events.length <= 20);
    const ids = new Set();
    for (const event of section.events) {
      require(keys(event, ["id", "subject", "status", "category", "reason", "evidence_spans", "evidence_complete"])
        && text(event.id, 32) && !ids.has(event.id) && text(event.subject, 160) && text(event.reason, 300)
        && ["counted", "duplicate", "held"].includes(event.status) && [...categories[name], null].includes(event.category)
        && typeof event.evidence_complete === "boolean" && Array.isArray(event.evidence_spans)
        && event.evidence_spans.length <= 3 && (!event.evidence_complete || event.evidence_spans.length > 0));
      ids.add(event.id);
      const seen = new Set();
      for (const span of event.evidence_spans) {
        require(keys(span, ["source", "text", "loc"]) && keys(span.loc, ["start", "end"])
          && text(span.text, 1000) && isLocatedEvidenceSpan(span, encounter));
        const source = {signed_note: encounter.signedNote, transcript: encounter.transcript, intake_text: encounter.intake}[span.source];
        require(typeof source === "string" && [...source].length <= 500_000
          && source.indexOf(span.text) === source.lastIndexOf(span.text));
        const key = JSON.stringify([span.source, span.loc.start, span.loc.end]);
        require(!seen.has(key)); seen.add(key);
      }
      if (event.status === "counted") require(event.evidence_complete && event.category !== null
        && event.evidence_spans.every((span) => span.source !== "intake_text"));
    }
    if (section.evidence_complete && !section.events.some((e) => e.status === "counted")) require(section.level === "straightforward");
  }
  return review;
}

export function clinicalSectionSummary(section) {
  if (!section.evidence_complete) return "Summary unavailable: retained source evidence is incomplete.";
  if (!section.events.some((e) => e.status === "counted")) return "No qualifying classification was established.";
  return `Element level: ${section.level}.`;
}

export function clinicalReviewText(review, encounter) {
  readClinicalReview(review, encounter);
  return ["MDM problems and management risk", `Rule: ${review.rule_version}`,
    ...Object.entries(clinicalSections).flatMap(([name, label]) => [
      `${label}: ${clinicalSectionSummary(review[name])}`, `Rule: ${review[name].rule_version}`,
      ...review[name].events.flatMap((event) => [
        `${event.status}: ${event.subject}. ${event.category ? `${clinicalCategoryLabels[event.category]}. ` : ""}${event.reason}`,
        ...event.evidence_spans.map((span) => `${span.source}, characters ${span.loc.start}–${span.loc.end}: ${span.text}`),
        ...(!event.evidence_complete ? ["Complete source evidence was not located."] : []),
      ]),
    ])].join("\n");
}
