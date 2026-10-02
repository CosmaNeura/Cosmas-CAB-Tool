const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const hash = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const uuid = (value) => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const text = (value, max) => typeof value === "string" && [...value].length <= max
  && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ud800-\udfff]/u.test(value);
const require = (condition) => { if (!condition) throw new Error("Staff review data could not be verified."); };
const kinds = { sms_reply: "Text reply", call_keypad: "Call keypad response", delivery_failure: "Delivery failure" };

function instant(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return false;
  const day = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  return Number.isFinite(Date.parse(value)) && Number.isFinite(day.getTime())
    && day.toISOString().slice(0, 10) === value.slice(0, 10);
}

export function reviewCounts(value) {
  return object(value) && count(value.pending) && count(value.held) && value.held <= value.pending
    ? { pending: value.pending, held: value.held } : null;
}

/** Whitelist the selected-case display contract; never project hidden routing data. */
export function readEventReview(value, chaseId) {
  require(object(value) && value.schemaVersion === "communications-review.v1" && value.chaseId === chaseId
    && typeof chaseId === "string" && chaseId.length > 0 && chaseId.length <= 160
    && count(value.totalPending) && count(value.heldPending) && value.heldPending <= value.totalPending
    && Array.isArray(value.items) && value.items.length === Math.min(50, value.totalPending)
    && value.truncated === (value.totalPending > 50));
  const items = value.items.map((item) => {
    require(object(item) && hash(item.ref) && hash(item.sourceHash) && Object.hasOwn(kinds, item.kind)
      && ["sms", "automated_voice"].includes(item.channel) && text(item.text, 1600)
      && instant(item.at) && text(item.status, 40) && item.status.length > 0
      && (item.disposition === null || text(item.disposition, 40))
      && typeof item.held === "boolean" && typeof item.canAttribute === "boolean"
      && ["pending", "attributed"].includes(item.reviewState)
      && count(item.mediaCount) && item.mediaCount <= 10);
    require((item.kind !== "sms_reply" || item.channel === "sms")
      && (item.kind !== "call_keypad" || item.channel === "automated_voice")
      && (item.kind !== "delivery_failure" || (item.text === "" && !item.held))
      && (!item.held || (item.kind === "sms_reply" && item.reviewState === "pending"))
      && (!item.canAttribute || item.held)
      && (item.canAttribute ? uuid(item.contactSnapshotId) : item.contactSnapshotId === null));
    return { ref: item.ref, sourceHash: item.sourceHash, kind: item.kind, channel: item.channel,
      text: item.text, at: item.at, status: item.status, disposition: item.disposition,
      held: item.held, canAttribute: item.canAttribute, contactSnapshotId: item.contactSnapshotId,
      reviewState: item.reviewState, mediaCount: item.mediaCount };
  });
  require(new Set(items.map((item) => item.ref)).size === items.length);
  const held = items.filter((item) => item.held).length;
  require(held <= value.heldPending && items.length - held <= value.totalPending - value.heldPending
    && (value.truncated || held === value.heldPending));
  return { chaseId, items, totalPending: value.totalPending, heldPending: value.heldPending, truncated: value.truncated };
}

export function eventReviewLabel(kind) { return kinds[kind] || "Communication event"; }
export function reviewDecisions(item) {
  return item.held ? [...(item.canAttribute ? ["attribute"] : []), "leave_unassigned"] : ["handled"];
}

export function eventReviewCommand(view, ref, decision, confirmed, actionId) {
  const item = view.items.find((entry) => entry.ref === ref);
  require(item && confirmed === true && uuid(actionId) && reviewDecisions(item).includes(decision));
  return Object.freeze({ chaseId: view.chaseId, actionId, eventRef: item.ref, sourceHash: item.sourceHash,
    decision, confirmed: true, contactSnapshotId: decision === "attribute" ? item.contactSnapshotId : null });
}

export function eventReviewError(failure) {
  if (failure?.status === 403) return "Your current access does not allow this review. Refresh before continuing.";
  if ([404, 409].includes(failure?.status)) return "This event or contact changed. Refresh and review the current information.";
  if (failure?.status === 422) return "This decision was not accepted. Refresh and review the available choices.";
  return "The review could not be confirmed. Retry the same decision to check its result.";
}
