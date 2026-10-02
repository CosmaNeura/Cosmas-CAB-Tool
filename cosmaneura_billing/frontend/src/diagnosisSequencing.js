const VERSION = "diagnosis-sequencing.2026-09-30.v1";
const BASIS = {
  resolved_explicit: "explicit_extracted", legacy_fallback: "legacy_first_captured",
  unresolved_explicit: "none", conflicting_explicit: "none", not_established: "none",
};
const KEYS = ["message", "requires_review", "rule_version", "selection_basis", "status"];

export function validDiagnosisSequencing(value) {
  return value != null && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify(KEYS)
    && value.rule_version === VERSION && Object.hasOwn(BASIS, value.status)
    && value.selection_basis === BASIS[value.status]
    && value.requires_review === (value.status !== "resolved_explicit")
    && typeof value.message === "string" && value.message.trim().length > 0
    && value.message.length <= 400 && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value.message);
}

export function sequencingMessage(value) {
  if (value === undefined) return "Primary diagnosis sequencing was not reported; confirm it in review.";
  return validDiagnosisSequencing(value) ? value.message
    : "Primary diagnosis sequencing could not be verified; confirm it in review.";
}

export function primaryBadge(value) {
  // Old responses remain readable, without upgrading their row order to proof.
  if (value === undefined) return "primary candidate";
  if (!validDiagnosisSequencing(value)) return null;
  if (value.status === "resolved_explicit") return "primary candidate";
  return value.status === "legacy_fallback" ? "provisional order" : null;
}
