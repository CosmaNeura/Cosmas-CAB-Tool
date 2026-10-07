export const SMS_MAX_LENGTH = 1000;

const STATUS = Object.freeze({
  prepared: "Prepared · not sent", authorized: "Authorized · awaiting dispatch", blocked: "Held · not sent",
  unknown: "Outcome unknown · review required", scheduled: "Scheduled", read: "Read",
  simulated: "Simulation created", accepted: "Accepted", queued: "Queued", sending: "Sending",
  sent: "Sent · delivery unconfirmed", delivered: "Delivered", undelivered: "Not delivered",
  failed: "Failed", rejected: "Rejected", received: "Received", initiated: "Starting",
  ringing: "Ringing", "in-progress": "Answered", in_progress: "Answered", answered: "Answered", completed: "Completed",
  busy: "Busy", "no-answer": "No answer", no_answer: "No answer", canceled: "Canceled",
});
const REASONS = Object.freeze({
  communications_channel_disabled: "This communication channel is unavailable for this workspace.",
  communications_test_recipients_unconfigured: "SMS test mode has no approved recipients. An administrator must configure consenting test recipients.",
  communications_test_recipient_not_allowed: "This contact is not an approved SMS test recipient. Verify the contact and ask an administrator to review the test-recipient list.",
  communications_contact_not_bound: "Verify a current contact before preparing outreach.",
  communications_contact_stale: "The contact changed or needs a fresh verification.",
  communications_contact_suppressed: "This recipient opted out of this channel.",
  communications_consent_unavailable: "Current contact evidence does not allow this channel.",
  communications_purpose_consent_required: "Record verified permission for this type of follow-up.",
  communications_policy_approval_required: "An administrator must approve the current outreach policy.",
  communications_approval_required: "This draft needs explicit staff authorization.",
  communications_authorization_unavailable: "Authorization availability was not reported. Refresh before authorizing; saved drafts remain available.",
  communications_approval_stale: "The approval no longer matches current contact, consent, or policy evidence. Prepare a new draft after review.",
  communications_approval_expired: "This approval expired. Review the current information and prepare a new draft.",
  communications_quiet_hours: "Held during the practice's quiet hours.",
  communications_touch_limit: "The contact-attempt limit has been reached.",
  communications_dispatch_disabled: "Outbound messaging and calls are disabled.",
  communications_direct_contract_blocked: "Direct-contract screening blocks outreach.",
  communications_case_blocked: "The current case status blocks outreach.",
  communications_staff_required: "Current staff authorization is required.",
  communications_admin_required: "Current administrator approval is required.",
  communications_sender_changed: "The sender changed. Verify the current setup.",
  communications_touch_evidence_invalid: "Contact history needs review before more outreach.",
  communications_message_requires_review: "Review the message content before preparing outreach.",
  communications_message_requires_identity_and_opt_out: "Include the practice name and Reply STOP instructions.",
  communications_operation_canceled: "This draft was canceled before dispatch.",
  communications_dispatch_already_started: "Dispatch already started. This operation cannot be recalled here.",
  communications_unknown_requires_resolution: "Investigate the earlier unknown outcome before any new text or call for this case.",
  communications_dispatch_in_progress: "The provider request is still running. Refresh after it finishes.",
});
export function communicationsReason(reason) { return Object.hasOwn(REASONS, reason) ? REASONS[reason] : "Current policy blocks this action. Refresh before continuing."; }
export const CALL_DISPOSITIONS = Object.freeze({
  callback_requested: "Callback requested", reports_submitted: "Reported submitted · verification needed",
  stop_calls: "Requested no further calls",
});
export const SMS_DEMO_EVENTS = Object.freeze([
  ["sms_sent", "Sent"], ["sms_delivered", "Delivered"], ["sms_failed", "Failed"],
]);
export const VOICE_DEMO_EVENTS = Object.freeze([
  ["voice_ringing", "Ringing"], ["voice_answered", "Answered"], ["voice_completed", "Completed"],
  ["voice_callback", "Keypad · request callback"],
  ["voice_reported_submitted", "Keypad · reported submitted"], ["voice_stop", "Keypad · stop calls"],
]);

const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const string = (value, max = 1000) => typeof value === "string" && value.length <= max;
const id = (value) => string(value, 160) && value.length > 0;
const code = (value) => string(value, 64) && /^[a-z][a-z0-9_-]*$/.test(value);
const nullableCode = (value) => value === null || code(value);
const channel = (value) => ["sms", "automated_voice"].includes(value);
const direction = (value) => ["inbound", "outbound"].includes(value);
const unique = (items, key) => new Set(items.map((item) => item[key])).size === items.length;
const uuid = (value) => string(value, 36) && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
function instant(value) {
  if (!string(value, 40) || value.endsWith("-00:00")) return false;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
  if (!match || Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4]) > 59
    || Number(match[6] || 0) > 23 || Number(match[7] || 0) > 59) return false;
  const parsed = new Date(value);
  const day = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && Number.isFinite(day.getTime())
    && day.toISOString().slice(0, 10) === value.slice(0, 10);
}
const require = (condition) => { if (!condition) throw new Error("Communications could not be verified."); };

const DELIVERY_PROBLEMS = new Set(["failed", "undelivered", "rejected", "unknown", "busy", "no-answer", "canceled"]);
const SUBMISSION_CODES = new Set(["transport_uncertain", "provider_uncertain", "invalid_provider_response", "provider_rejected"]);
const providerCode = (value) => typeof value === "string" && value === value.trim() && /^[0-9]{1,6}$/.test(value);
function readDeliveryError(value, status, durable, callback = false) {
  if (value == null) return null; // Earlier servers/history have no code.
  require(durable && DELIVERY_PROBLEMS.has(status) && object(value)
    && Object.keys(value).sort().join(",") === "code,source"
    && ["submission", "callback"].includes(value.source) && (!callback || value.source === "callback")
    && typeof value.code === "string" && (providerCode(value.code)
      || value.source === "submission" && SUBMISSION_CODES.has(value.code)));
  return { code: value.code, source: value.source };
}

export function communicationsDeliveryError(issue, status) {
  if (!issue || !DELIVERY_PROBLEMS.has(status)) return null;
  const checked = readDeliveryError(issue, status, true);
  if (status === "unknown") return `${providerCode(checked.code) ? `Provider code ${checked.code}: ` : ""}Submission could not be confirmed. Check the provider's records; this operation will not be sent again.`;
  const explanations = {
    "30034": "The sender's carrier registration is incomplete. Ask an administrator to check A2P registration before another send.",
    "21610": "The recipient has opted out. Keep outreach stopped and review consent.",
    "30003": "The recipient's phone was unreachable. Verify the contact before considering another message.",
    "30005": "The destination number was not recognized. Verify the contact.",
    "30006": "The destination cannot receive this message. Verify that the contact supports SMS.",
    "30007": "The message was filtered. Ask an administrator to review the sender and message; do not repeatedly resend.",
  };
  return providerCode(checked.code)
    ? `Provider code ${checked.code}: ${explanations[checked.code] || "The provider reported a delivery problem. Ask an administrator to review this code before another send."}`
    : "The provider rejected this request. Ask an administrator to review the sender configuration before another send.";
}

/** Project only the selected case; provider IDs and staff identifiers stay private. */
export function readCommunications(value, chaseId) {
  const durable = value?.mode === "durable";
  require(object(value) && value.schemaVersion === "chase-communications.v1"
    && value.chaseId === chaseId && ["simulated", "durable"].includes(value.mode)
    && Array.isArray(value.operations) && value.operations.length <= (durable ? 200 : 500)
    && Array.isArray(value.events) && value.events.length <= (durable ? 200 : 500));
  // Older servers must not implicitly enable calling. History remains visible
  // independently of the channels that permit new outbound work.
  const enabledChannels = value.enabledChannels === undefined ? ["sms"] : value.enabledChannels;
  require(Array.isArray(enabledChannels) && enabledChannels.length <= 2
    && enabledChannels.every(channel) && new Set(enabledChannels).size === enabledChannels.length);
  const operations = value.operations.map((item) => {
    require(object(item) && id(item.operationId) && item.caseId === chaseId && channel(item.channel)
      && item.direction === "outbound" && string(item.text, 4000) && code(item.status)
      && item.simulated === !durable && instant(item.createdAt) && typeof item.needsStaffReview === "boolean"
      && (!durable || uuid(item.operationId)));
    const held = durable && item.status === "blocked";
    require(!held || (code(item.blockedReason) && instant(item.lastCheckedAt)));
    require(!durable || ((item.cancelable === undefined || typeof item.cancelable === "boolean")
      && (!item.cancelable || ["prepared", "authorized", "blocked"].includes(item.status))
      && (item.canceledAt == null || (instant(item.canceledAt) && item.status === "canceled" && item.cancelable === false))));
    const resolution = durable ? item.unknownResolution : null;
    require(!durable || (item.canResolveUnknown === undefined || typeof item.canResolveUnknown === "boolean"));
    require(!item.canResolveUnknown || (durable && item.status === "unknown" && resolution == null));
    require(resolution == null || (object(resolution) && item.status === "unknown" && item.canResolveUnknown === false
      && ["confirmed_delivered", "confirmed_not_sent", "confirmed_attempted"].includes(resolution.decision)
      && string(resolution.vendorReference, 200) && /^[A-Za-z0-9_.:/-]{1,200}$/.test(resolution.vendorReference)
      && instant(resolution.resolvedAt)));
    return { operationId: item.operationId, channel: item.channel, direction: item.direction,
      text: item.text, status: item.status, createdAt: item.createdAt, needsStaffReview: item.needsStaffReview,
      blockedReason: held ? item.blockedReason : null, lastCheckedAt: held ? item.lastCheckedAt : null,
      cancelable: durable && item.cancelable === true, canceledAt: durable ? item.canceledAt || null : null,
      canResolveUnknown: durable && item.canResolveUnknown === true,
      deliveryError: readDeliveryError(item.deliveryError, item.status, durable),
      unknownResolution: resolution ? { decision: resolution.decision, vendorReference: resolution.vendorReference,
        resolvedAt: resolution.resolvedAt } : null };
  });
  const events = value.events.map((item) => {
    require(object(item) && id(item.eventId) && code(item.kind) && (direction(item.direction) || item.direction === "callback" || (durable && item.direction === "staff"))
      && channel(item.channel) && (item.text === null || string(item.text, 4000))
      && nullableCode(item.status) && nullableCode(item.disposition) && instant(item.occurredAt)
      && typeof item.needsStaffReview === "boolean"
      && (item.operationId == null || id(item.operationId))
      && (item.mediaCount == null || (Number.isSafeInteger(item.mediaCount) && item.mediaCount >= 0 && item.mediaCount <= 10)));
    return { eventId: item.eventId, kind: item.kind, channel: item.channel, direction: item.direction,
      text: item.text, status: item.status, disposition: item.disposition,
      deliveryError: readDeliveryError(item.deliveryError, item.status, durable && item.direction === "callback", true),
      occurredAt: item.occurredAt, needsStaffReview: item.needsStaffReview,
      operationId: item.operationId || null, mediaCount: item.mediaCount || 0 };
  });
  require(unique(operations, "operationId") && unique(events, "eventId")
    && object(value.suppression) && typeof value.suppression.sms === "boolean"
    && typeof value.suppression.automatedVoice === "boolean"
    && Number.isSafeInteger(value.heldInboundCount) && value.heldInboundCount >= 0
    && object(value.actions));
  require(value.reviewPendingCount === undefined || (durable && Number.isSafeInteger(value.reviewPendingCount)
    && value.reviewPendingCount >= value.heldInboundCount));
  const actions = {};
  for (const name of ["sms", "automatedVoice"]) {
    const action = value.actions[name];
    require(object(action) && typeof action.allowed === "boolean"
      && (action.reason === null || string(action.reason, 500))
      && !(value.suppression[name] && action.allowed));
    const enabled = enabledChannels.includes(name === "sms" ? "sms" : "automated_voice");
    actions[name] = { allowed: enabled && action.allowed,
      reason: enabled ? action.reason : "communications_channel_disabled" };
    if (durable && name === "sms") {
      // Additive metadata: old/malformed responses retain the conversation and
      // composer, but cannot claim that a new authorization is currently safe.
      const auth = action.authorization;
      const valid = object(auth) && Object.keys(auth).sort().join(",") === "allowed,reason"
        && typeof auth.allowed === "boolean"
        && (auth.allowed ? auth.reason === "" || auth.reason === null : code(auth.reason));
      actions.sms.authorization = !actions.sms.allowed
        ? { allowed: false, reason: actions.sms.reason }
        : valid ? { allowed: auth.allowed, reason: auth.reason }
          : { allowed: false, reason: "communications_authorization_unavailable" };
    }
  }
  require(Array.isArray(value.templates) && value.templates.length <= 20 && unique(value.templates, "id")
    && value.templates.every((item) => object(item) && id(item.id) && string(item.label, 100)
      && string(item.text, SMS_MAX_LENGTH)) && string(value.callScript, 4000));
  let contact = null, bindings = {}, policy = null;
  if (durable) {
    require(value.contact === null || object(value.contact));
    if (value.contact) {
      const c = value.contact;
      require(uuid(c.snapshotId) && string(c.source, 128) && instant(c.verifiedAt)
        && Array.isArray(c.routes) && c.routes.length <= 2 && unique(c.routes, "channel")
        && c.routes.every((r) => object(r) && channel(r.channel) && typeof r.destination === "string"
          && /^\+[1-9][0-9]{7,14}$/.test(r.destination) && ["granted", "denied", "unknown", "expired", "not_required"].includes(r.consentStatus)));
      contact = { snapshotId: c.snapshotId, source: c.source, verifiedAt: c.verifiedAt,
        routes: c.routes.map(({ channel: name, destination, consentStatus }) => ({ channel: name, destination, consentStatus })) };
    }
    require(object(value.bindings) && object(value.policy) && typeof value.truncated === "boolean");
    for (const name of ["sms", "automated_voice"]) {
      const binding = value.bindings[name];
      if (binding !== undefined) {
        require(object(binding) && uuid(binding.bindingId) && typeof binding.consentGranted === "boolean"
          && contact?.routes.some((r) => r.channel === name));
        bindings[name] = { bindingId: binding.bindingId, consentGranted: binding.consentGranted };
      }
    }
    const p = value.policy;
    require(string(p.hash, 64) && /^[0-9a-f]{64}$/.test(p.hash)
      && [p.approved, p.canApprove, p.outboundEnabled].every((v) => typeof v === "boolean")
      && string(p.practiceName, 80) && p.practiceName.length > 0 && string(p.timezone, 80)
      && Array.isArray(p.quietHours) && p.quietHours.length === 2 && p.quietHours[0] !== p.quietHours[1]
      && p.quietHours.every((n) => Number.isInteger(n) && n >= 0 && n <= 23)
      && Number.isInteger(p.maxTotalTouches) && p.maxTotalTouches >= 1 && p.maxTotalTouches <= 50
      && Number.isInteger(p.maxTouches24h) && p.maxTouches24h >= 1 && p.maxTouches24h <= 10);
    policy = { hash: p.hash, approved: p.approved, canApprove: p.canApprove, outboundEnabled: p.outboundEnabled,
      practiceName: p.practiceName, timezone: p.timezone, quietHours: [...p.quietHours],
      maxTotalTouches: p.maxTotalTouches, maxTouches24h: p.maxTouches24h };
    if (actions.sms.authorization.allowed && !p.outboundEnabled) {
      actions.sms.authorization = { allowed: false, reason: "communications_dispatch_disabled" };
    }
  }
  return { chaseId, mode: value.mode, enabledChannels: [...enabledChannels], operations, events, actions, contact, bindings, policy,
    truncated: durable ? value.truncated : false,
    suppression: { sms: value.suppression.sms, automatedVoice: value.suppression.automatedVoice },
    heldInboundCount: value.heldInboundCount, reviewPendingCount: value.reviewPendingCount ?? null, callScript: value.callScript,
    templates: value.templates.map(({ id: key, label, text }) => ({ id: key, label, text })) };
}

export function communicationsStatus(status) { return Object.hasOwn(STATUS, status) ? STATUS[status] : "Status unavailable"; }
export function communicationsDisposition(value) { return Object.hasOwn(CALL_DISPOSITIONS, value) ? CALL_DISPOSITIONS[value] : null; }

export function smsThread(view) {
  return [
    ...view.operations.filter((item) => item.channel === "sms" && item.direction === "outbound")
      .map((item) => ({ id: `operation:${item.operationId}`, direction: "outbound", text: item.text,
        status: item.status, at: item.createdAt, needsStaffReview: item.needsStaffReview,
        operationId: item.operationId, blockedReason: item.blockedReason, lastCheckedAt: item.lastCheckedAt,
        cancelable: item.cancelable, canceledAt: item.canceledAt, canResolveUnknown: item.canResolveUnknown,
        unknownResolution: item.unknownResolution, deliveryError: item.deliveryError })),
    ...view.events.filter((item) => item.channel === "sms" && item.direction === "inbound" && (item.text || item.mediaCount))
      .map((item) => ({ id: `event:${item.eventId}`, direction: "inbound", text: item.text,
        status: item.status || "received", at: item.occurredAt, needsStaffReview: item.needsStaffReview, mediaCount: item.mediaCount })),
  ].sort((left, right) => Date.parse(left.at) - Date.parse(right.at));
}

export function communicationError(error, mode = "simulated") {
  const action = mode === "durable" ? "action" : "simulation";
  if (error?.status === 403) return "Communications are blocked by the current case or access policy. Refresh before continuing.";
  if (error?.status === 404) return "Communications are unavailable for this case.";
  if (error?.status === 409) return "This action is no longer allowed. Refresh communications before continuing.";
  if ([400, 413, 422].includes(error?.status)) return `The ${action} was not accepted. Check the selected information and message.`;
  if (error?.status === 429) return `The service is busy. Retry the same ${action} shortly.`;
  return `The ${action} could not be confirmed. Retry the same ${action} safely.`;
}
