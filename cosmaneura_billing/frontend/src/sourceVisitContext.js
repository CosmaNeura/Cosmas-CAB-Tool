// Source metadata accompanies the selected encounter; it never edits a saved header.
const schema = "cab.source-visit-context.v1";
const keys = ["expectedSource", "noteStatus", "origin", "schemaVersion", "serviceDate", "sourceSha256", "statusMeaning"];
const athenaSources = new Set(["athena_staff_review_mock", "athena_staff_review_preview_synthetic", "athena_staff_review_operational"]);
const canonical = (v) => JSON.stringify(v, (_, value) => value && !Array.isArray(value) && typeof value === "object"
  ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : value);
const hash = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const status = (v) => typeof v === "string" && [...v].length >= 1 && [...v].length <= 128
  && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(v);
const require = (ok) => { if (!ok) throw new Error("invalid-source-visit-context"); };

export function validServiceDate(value) {
  if (typeof value !== "string" || !/^(?!0000)\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(value + "T00:00:00Z");
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export function readSourceVisitContext(value, expectedSource) {
  if (value === undefined) return null; // Older detail responses omit this additive DTO.
  require(value && typeof value === "object" && !Array.isArray(value)
    && canonical(Object.keys(value).sort()) === canonical(keys) && value.schemaVersion === schema
    && expectedSource && canonical(value.expectedSource) === canonical(expectedSource));
  if (value.origin === "unavailable") {
    require(value.sourceSha256 === null && value.serviceDate === null && value.noteStatus === null
      && value.statusMeaning === "unavailable");
  } else {
    require(hash(value.sourceSha256) && (value.serviceDate === null || validServiceDate(value.serviceDate)));
    if (value.origin === "provider_manifest") require(expectedSource.ingestionSource === "provider_completed_note"
      && value.statusMeaning === "provider_attested" && ["completed", "signed"].includes(value.noteStatus));
    else require(value.origin === "athena_import" && athenaSources.has(expectedSource.ingestionSource)
      && value.statusMeaning === "source_reported" && (value.noteStatus === null || status(value.noteStatus)));
  }
  return structuredClone(value);
}

export function boundSourceVisitContext(value, expectedSource) {
  // Invalid/stale context must not contaminate an otherwise valid saved header.
  try { return readSourceVisitContext(value, expectedSource); } catch { return null; }
}

export function sourceVisitContextDisplay(value, expectedSource) {
  const context = boundSourceVisitContext(value, expectedSource);
  return {
    serviceDate: context?.serviceDate ?? "Unavailable from source",
    noteStatus: context?.origin === "provider_manifest"
      ? context.noteStatus === "signed" ? "Signed (provider-confirmed)" : "Completed (provider-confirmed)"
      : context?.noteStatus ?? "Unavailable from source",
    qualification: context?.statusMeaning === "provider_attested"
      ? "Provider confirmation; upstream signature not independently verified."
      : context?.statusMeaning === "source_reported"
        ? "Source-reported status; upstream signature not independently verified." : null,
  };
}

export function sourceVisitContextText(value, expectedSource) {
  const display = sourceVisitContextDisplay(value, expectedSource);
  return [`Date of service: ${display.serviceDate}`, `Note status: ${display.noteStatus}`,
    ...(display.qualification ? [display.qualification] : [])].join("\n");
}
