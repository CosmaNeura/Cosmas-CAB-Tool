const keys = ["schemaVersion", "encounterVersion", "bundleSha256", "ingestionSource", "status", "revisionNumber", "reviewedRevision"];
const statuses = new Set(["not_generated", "awaiting_review", "reviewed", "review_required", "unavailable"]);
const revision = (value) => Number.isInteger(value) && value >= 0 && value <= 100;

/** Additive list metadata never supplies a header, source admission, or code. */
export function readHeaderProgress(value) {
  if (!value || Array.isArray(value) || ![Object.prototype, null].includes(Object.getPrototypeOf(value))
      || Object.keys(value).sort().join() !== [...keys].sort().join()
      || value.schemaVersion !== "billing-header-progress.v1"
      || !Number.isInteger(value.encounterVersion) || value.encounterVersion < 1
      || typeof value.bundleSha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.bundleSha256)
      || typeof value.ingestionSource !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(value.ingestionSource)
      || !statuses.has(value.status)) return null;
  const {status, revisionNumber: current, reviewedRevision: reviewed} = value;
  if (["not_generated", "unavailable"].includes(status) ? current !== null || reviewed !== null
    : !revision(current) || (status === "awaiting_review" ? reviewed !== null
      : !revision(reviewed) || (status === "reviewed" ? reviewed !== current : reviewed >= current))) return null;
  return {...value};
}

export function headerProgressFromView(view, pin) {
  if (!view?.header?.expectedSource || !pin || view.header.expectedSource.encounterVersion !== pin.encounterVersion
      || view.header.expectedSource.bundleSha256 !== pin.bundleSha256
      || view.header.expectedSource.ingestionSource !== pin.ingestionSource) return null;
  return readHeaderProgress({schemaVersion: "billing-header-progress.v1",
    encounterVersion: pin.encounterVersion, bundleSha256: pin.bundleSha256,
    ingestionSource: pin.ingestionSource,
    status: view.signoff ? "reviewed" : view.previousSignoff ? "review_required" : "awaiting_review",
    revisionNumber: view.revisionNumber,
    reviewedRevision: (view.signoff || view.previousSignoff)?.headerRevision ?? null});
}

export function headerProgressLabel(value) {
  const progress = readHeaderProgress(value);
  if (!progress || progress.status === "unavailable") return "Saved progress unavailable";
  if (progress.status === "not_generated") return "No header saved for current source";
  const suffix = {reviewed: "coder review recorded", review_required: "fresh coder review required",
    awaiting_review: "awaiting coder review"}[progress.status];
  return `Revision ${progress.revisionNumber} · ${suffix}`;
}

/** A later editor receipt or invalidation always wins over a queued list read. */
export function applyProgressRefresh(current, entries, capturedVersion, currentVersion) {
  return capturedVersion === currentVersion
    ? Object.fromEntries(entries.map((entry) => [entry.id, readHeaderProgress(entry.headerProgress)])) : current;
}
