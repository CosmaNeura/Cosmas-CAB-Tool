const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const count = (v) => Number.isSafeInteger(v) && v >= 0;
const hash = (v) => typeof v === "string" && /^[0-9a-f]{64}$/.test(v);
const uuid = (v) => typeof v === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const id = (v) => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v);
const text = (v, max) => typeof v === "string" && [...v].length <= max
  && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ud800-\udfff]/u.test(v);
const require = (v) => { if (!v) throw new Error("Unassigned text review data could not be verified."); };
const instant = (v) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v)
  && Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0, 10) === v.slice(0, 10);

function page(value, limit) {
  require(object(value) && Array.isArray(value.items) && value.items.length <= limit
    && typeof value.hasMore === "boolean" && (value.hasMore
      ? value.items.length === limit && typeof value.nextCursor === "string" && /^[A-Za-z0-9_-]{1,700}$/.test(value.nextCursor)
      : value.nextCursor === null));
  return { hasMore: value.hasMore, nextCursor: value.nextCursor };
}

export function readUnassignedCount(value) {
  require(object(value) && value.schemaVersion === "communications-unassigned-count.v1" && count(value.pendingCount));
  return value.pendingCount;
}

/** Copy only the display contract; routing, account and vendor identifiers stay server-side. */
export function readUnassignedTexts(value, state, limit = 50) {
  const paging = page(value, limit);
  require(value.schemaVersion === "communications-unassigned.v1" && ["pending", "reviewed"].includes(state)
    && value.state === state && count(value.pendingCount));
  const items = value.items.map((v) => {
    require(object(v) && hash(v.ref) && hash(v.sourceHash) && v.kind === "sms_reply" && v.channel === "sms"
      && text(v.text, 1600) && instant(v.at) && text(v.status, 40) && v.status.length > 0
      && (v.disposition === null || text(v.disposition, 40)) && count(v.mediaCount) && v.mediaCount <= 10
      && typeof v.senderHint === "string" && /^Ending \d{4}$/.test(v.senderHint));
    require(state === "pending" ? v.reviewState === "pending" && v.reviewedAt === null && v.attributedCaseId === null
      : instant(v.reviewedAt) && (v.reviewState === "attributed" ? id(v.attributedCaseId)
        : v.reviewState === "left_unassigned" && v.attributedCaseId === null));
    return { ref: v.ref, sourceHash: v.sourceHash, text: v.text, at: v.at, status: v.status,
      disposition: v.disposition, mediaCount: v.mediaCount, senderHint: v.senderHint,
      reviewState: v.reviewState, reviewedAt: v.reviewedAt, attributedCaseId: v.attributedCaseId };
  });
  require(new Set(items.map((v) => v.ref)).size === items.length);
  return { ...paging, state, pendingCount: value.pendingCount, items };
}

export function readUnassignedCases(value, item, limit = 20) {
  const paging = page(value, limit);
  require(value.schemaVersion === "communications-unassigned-cases.v1" && value.eventRef === item.ref && value.sourceHash === item.sourceHash);
  const items = value.items.map((v) => {
    require(object(v) && id(v.chaseId) && text(v.patientLabel, 160) && v.patientLabel.length > 0
      && text(v.state, 40) && text(v.bucket, 40) && uuid(v.contactSnapshotId) && instant(v.verifiedAt));
    return { chaseId: v.chaseId, patientLabel: v.patientLabel, state: v.state, bucket: v.bucket,
      contactSnapshotId: v.contactSnapshotId, verifiedAt: v.verifiedAt };
  });
  require(new Set(items.map((v) => v.chaseId)).size === items.length);
  return { ...paging, items };
}

export function unassignedCommand(item, decision, selectedCase, confirmed, actionId) {
  require(item?.reviewState === "pending" && hash(item.ref) && hash(item.sourceHash) && confirmed === true && uuid(actionId));
  require(decision === "attribute" ? id(selectedCase?.chaseId) && uuid(selectedCase?.contactSnapshotId)
    : decision === "leave_unassigned" && selectedCase === null);
  return Object.freeze({ actionId, eventRef: item.ref, sourceHash: item.sourceHash, decision, confirmed: true,
    chaseId: selectedCase?.chaseId ?? null, contactSnapshotId: selectedCase?.contactSnapshotId ?? null });
}

export function readUnassignedReceipt(value, command) {
  require(object(value) && value.schemaVersion === "communications-unassigned-decision.v1" && count(value.pendingCount));
  const r = value.receipt;
  require(object(r) && ["actionId", "eventRef", "sourceHash", "decision", "chaseId"].every((key) => r[key] === command[key]) && instant(r.reviewedAt));
  return { pendingCount: value.pendingCount, reviewedAt: r.reviewedAt };
}
