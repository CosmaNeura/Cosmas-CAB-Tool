import { sequencingMessage } from "./diagnosisSequencing.js";

const SOURCE_LABELS = Object.freeze({
  synthetic_demo_release_candidate: "Validated synthetic release-candidate fixture",
  athena_preview_synthetic: "Athena Preview synthetic fixture",
  athena_staff_review_preview_synthetic: "Reviewed Athena Preview synthetic encounter",
  athena_staff_review_mock: "Local Athena workflow simulation",
  operational: "Operational source",
});

const SYNTHETIC_SOURCES = new Set([
  "synthetic_demo_release_candidate",
  "athena_preview_synthetic",
  "athena_staff_review_preview_synthetic",
  "athena_staff_review_mock",
]);

const EVIDENCE_SOURCES = Object.freeze({
  signed_note: "signedNote",
  transcript: "transcript",
  intake_text: "intake",
});

export function isSyntheticDemoEncounter(encounter) {
  return encounter?.readOnly === true && SYNTHETIC_SOURCES.has(encounter.dataClass);
}

export function sourceLabel(dataClass) {
  return SOURCE_LABELS[dataClass] || "Server-verified source";
}

export function visitTypeLabel(value) {
  if (!value) return "Not specified";
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function completenessLabel(report) {
  if (!report) return "Not checked";
  if (report.rulepack == null) return "No rule pack";
  const overrideCount = (report.results || []).filter(
    (item) => item.provider_override?.overridden === true,
  ).length;
  if (report.complete === true) {
    return overrideCount > 0
      ? `Cleared with ${overrideCount} reasoned override${overrideCount === 1 ? "" : "s"}`
      : "All requirements found";
  }
  const unresolved = (report.results || []).filter(
    (item) => item.status !== "present" && item.provider_override?.overridden !== true,
  ).length;
  return unresolved === 1 ? "1 blocker" : `${unresolved} blockers`;
}

export function isValidEncounterVersion(value) {
  return Number.isInteger(value) && value >= 1;
}

export function isCurrentEncounterResult(result, encounter) {
  return (
    isValidEncounterVersion(encounter?.encounterVersion)
    && isValidEncounterVersion(result?.encounterVersion)
    && result.encounterVersion === encounter.encounterVersion
  );
}

export function isLocatedEvidenceSpan(span, encounter) {
  if (!span || !encounter) return false;
  const encounterField = EVIDENCE_SOURCES[span.source];
  const sourceText = encounterField ? encounter[encounterField] : null;
  const start = span.loc?.start;
  const end = span.loc?.end;
  const sourceCharacters = typeof sourceText === "string" ? Array.from(sourceText) : [];
  return (
    typeof sourceText === "string"
    && typeof span.text === "string"
    && span.text.trim().length > 0
    && Number.isInteger(start)
    && Number.isInteger(end)
    && start >= 0
    && end > start
    && end <= sourceCharacters.length
    && sourceCharacters.slice(start, end).join("") === span.text
  );
}

export function suggestedCandidates(result, encounter) {
  if (!result || result.degraded) return [];
  return [...(result.cpt_candidates || []), ...(result.icd_candidates || [])]
    .filter((candidate) => (
      candidate.status === "suggested"
      && Array.isArray(candidate.evidence_spans)
      && candidate.evidence_spans.some(
        (span) => isLocatedEvidenceSpan(span, encounter),
      )
    ));
}

export function evidenceSupportedEm(result, encounter) {
  if (!result?.em || result.em.status !== "suggested") return null;
  const code = result.em.suggested_level;
  return suggestedCandidates(result, encounter).find((candidate) => candidate.code === code) || null;
}

export function preparedBillingBlock(result, encounter) {
  if (!result || result.degraded || !encounter) return "";
  if (!isCurrentEncounterResult(result, encounter)) return "";
  const suggested = suggestedCandidates(result, encounter);
  const cpt = suggested
    .filter((candidate) => (result.cpt_candidates || []).includes(candidate))
    .map((candidate) => candidate.code);
  const icd = suggested
    .filter((candidate) => (result.icd_candidates || []).includes(candidate))
    .map((candidate) => candidate.code);
  const completeness = completenessLabel(result.completeness);
  const supportedEm = evidenceSupportedEm(result, encounter);

  return [
    "COSMAS CAB — PREPARED BILLING REFERENCE",
    "STATUS: PREPARED FOR HUMAN REVIEW — NOT TRANSMITTED",
    `Encounter: ${encounter.encounterId}`,
    `Encounter version: ${encounter.encounterVersion}`,
    `Source: ${sourceLabel(result.dataClass || encounter.dataClass)}`,
    `Submitted E/M: ${result.em?.provider_selected_level || "None recorded"}`,
    `Evidence-supported E/M: ${supportedEm?.code || "None suggested"}`,
    `CPT / E/M suggestions: ${cpt.join(", ") || "None"}`,
    `ICD-10-CM suggestions: ${icd.join(", ") || "None"}`,
    `Primary diagnosis sequencing: ${sequencingMessage(result.diagnosis_sequencing)}`,
    `Documentation completeness: ${completeness}`,
    `Engine / ruleset bundle: ${result.engine_version || "Not reported"}`,
    `Inference: ${result.inferenceMode || "Not reported"}${result.inferenceModel ? ` · ${result.inferenceModel}` : ""}`,
    `Clinical note modified by CAB: ${result.clinical_note_modified === false ? "No" : "Not confirmed"}`,
    "Human review required. Nothing was sent to Athena, an EHR, a clearinghouse, or a payer.",
  ].join("\n");
}
