import assert from "node:assert/strict";
import test from "node:test";
import { communicationError, communicationsDeliveryError, communicationsDisposition, communicationsReason, communicationsStatus, readCommunications, smsThread } from "./caseCommunications.js";

const at = "2026-09-26T14:00:00.123456+00:00";
const operation = (extra = {}) => ({ operationId: "one", caseId: "case-a", channel: "sms", direction: "outbound",
  text: "Synthetic reminder", status: "delivered", simulated: true, createdAt: at, needsStaffReview: false, ...extra });
const event = (extra = {}) => ({ eventId: "reply", kind: "inbound_sms", channel: "sms", direction: "inbound",
  text: "Synthetic callback request", status: null, disposition: null, occurredAt: at, needsStaffReview: true, ...extra });
const payload = (extra = {}) => ({ schemaVersion: "chase-communications.v1", chaseId: "case-a", mode: "simulated",
  operations: [], events: [], suppression: { sms: false, automatedVoice: false }, heldInboundCount: 0,
  actions: { sms: { allowed: true, reason: "" }, automatedVoice: { allowed: true, reason: "" } },
  templates: [{ id: "reminder", label: "Reminder", text: "Synthetic reminder" }], callScript: "Synthetic script", ...extra });

test("case-bound projection drops provider IDs, contacts, and actor metadata", () => {
  const view = readCommunications(payload({ operations: [operation({ providerSid: "private-sid", fromNumber: "private-number", actorId: "private-actor" })],
    events: [event({ providerSid: "private-sid", held: false })] }), "case-a");
  assert.equal(view.operations.length, 1);
  assert.equal(JSON.stringify(view).includes("private"), false);
  assert.deepEqual(readCommunications(payload(), "case-a").operations, []);
  for (const change of [{ chaseId: "case-b" }, { mode: "live" }, { schemaVersion: "future" },
    { operations: [operation({ caseId: "case-b" })] }, { operations: [operation({ simulated: false })] },
    { operations: [operation({ direction: "inbound" })] }]) assert.throws(() => readCommunications(payload(change), "case-a"));
});

test("callback events update status without duplicate outgoing message bubbles", () => {
  const view = readCommunications(payload({ operations: [operation()], events: [
    event({ eventId: "created", direction: "outbound", kind: "outbound_created", text: "Synthetic reminder" }),
    event({ eventId: "delivered", direction: "callback", kind: "status", text: "", status: "delivered", needsStaffReview: false }),
    event(),
  ] }), "case-a");
  const thread = smsThread(view);
  assert.equal(thread.length, 2);
  assert.equal(thread[0].status, "delivered");
  assert.equal(thread[1].status, "received");
  assert.equal(thread[1].needsStaffReview, true);
});

test("status and keypad labels preserve uncertainty and explicit staff verification", () => {
  assert.equal(communicationsStatus("sent"), "Sent · delivery unconfirmed");
  assert.equal(communicationsStatus("in_progress"), "Answered");
  assert.equal(communicationsStatus("no_answer"), "No answer");
  assert.equal(communicationsStatus("unknown"), "Outcome unknown · review required");
  for (const value of ["future_status", "__proto__", undefined]) assert.equal(communicationsStatus(value), "Status unavailable");
  assert.match(communicationsDisposition("reports_submitted"), /verification needed/);
  assert.equal(communicationsDisposition("stop_calls"), "Requested no further calls");
  assert.equal(communicationsDisposition("__proto__"), null);
});

test("malformed history fails closed instead of showing a fabricated empty conversation", () => {
  for (const change of [{ events: [event(), event()] }, { operations: [operation(), operation()] },
    { events: [event({ direction: "private-data" })] }, { heldInboundCount: -1 }, { heldInboundCount: NaN },
    { operations: [operation({ createdAt: "2026-09-26T24:00:00Z" })] },
    { events: [event({ occurredAt: "2026-02-30T12:00:00Z" })] },
    { events: [event({ occurredAt: "2026-09-26T12:00:00" })] },
    { operations: [operation({ text: null })] }, { templates: [{ id: "x", text: "No label" }] },
    { suppression: { sms: true, automatedVoice: false } }, { actions: { sms: { allowed: true } } }]) {
    assert.throws(() => readCommunications(payload(change), "case-a"));
  }
});

test("suppressed and blocked cases retain inbound review without granting a new action", () => {
  const view = readCommunications(payload({ suppression: { sms: true, automatedVoice: false }, events: [event({ text: "STOP" })],
    actions: { sms: { allowed: false, reason: "This contact has opted out." }, automatedVoice: { allowed: false, reason: "This case is closed." } } }), "case-a");
  assert.equal(smsThread(view)[0].text, "STOP");
  assert.equal(view.actions.sms.allowed, false);
  assert.equal(view.actions.automatedVoice.allowed, false);
});

test("errors do not reflect provider response text", () => {
  for (const status of [403, 404, 409, 422, 429, 500, undefined]) {
    assert.equal(communicationError({ status, message: "PRIVATE-CANARY" }).includes("PRIVATE"), false);
  }
  assert.match(communicationError({ status: 503 }), /Retry the same/);
});

test("test-recipient holds give staff a specific safe next step", () => {
  assert.match(communicationsReason("communications_test_recipients_unconfigured"), /no approved recipients/);
  assert.match(communicationsReason("communications_test_recipient_not_allowed"), /not an approved SMS test recipient/);
  for (const reason of ["communications_test_recipients_unconfigured", "communications_test_recipient_not_allowed"])
    assert.match(communicationsReason(reason), /administrator/);
});


const uuid = "11111111-1111-4111-8111-111111111111";
const durable = (extra = {}) => payload({
  mode: "durable", truncated: false,
  contact: { snapshotId: uuid, source: "synthetic-staff-review", verifiedAt: at,
    routes: [{ channel: "sms", destination: "+12025550100", consentStatus: "unknown" }] },
  bindings: { sms: { bindingId: uuid, consentGranted: true } },
  policy: { hash: "a".repeat(64), approved: true, canApprove: false, outboundEnabled: false,
    practiceName: "Synthetic Practice", timezone: "UTC", quietHours: [21, 8], maxTotalTouches: 8, maxTouches24h: 2 },
  ...extra,
});

test("unanswered and invalid historical keypad responses remain reviewable in a texting-only workspace", () => {
  const view = readCommunications(durable({ enabledChannels: ["sms"], events: [
    event({eventId: "silent-call", channel: "automated_voice", kind: "voice_gather", direction: "callback",
      text: "", status: "in-progress", disposition: "no_input"}),
    event({eventId: "invalid-key", channel: "automated_voice", kind: "voice_gather", direction: "callback",
      text: "", status: "in-progress", disposition: "invalid_input"}),
  ] }), "case-a");
  assert.deepEqual(view.events.map((item) => communicationsDisposition(item.disposition)),
    ["No keypad response", "Unrecognized keypad response"]);
  assert.ok(view.events.every((item) => item.needsStaffReview));
  assert.equal(view.actions.automatedVoice.allowed, false);
  assert.deepEqual(view.enabledChannels, ["sms"]);
  assert.deepEqual(smsThread(view), []);
});

test("delivery error codes survive safe projection without vendor text or new authority", () => {
  const issue = { code: "30034", source: "callback" };
  const view = readCommunications(durable({ operations: [operation({ operationId: uuid, simulated: false,
    status: "undelivered", cancelable: false, deliveryError: issue, providerMessage: "PRIVATE-CANARY" })],
    events: [event({ direction: "callback", kind: "sms_status", status: "undelivered", deliveryError: issue,
      providerMessage: "PRIVATE-CANARY" })] }), "case-a");
  assert.deepEqual(smsThread(view)[0].deliveryError, issue);
  assert.deepEqual(view.events[0].deliveryError, issue);
  assert.equal(view.operations[0].cancelable, false);
  assert.equal(JSON.stringify(view).includes("PRIVATE-CANARY"), false);
  assert.match(communicationsDeliveryError(issue, "undelivered"), /Provider code 30034:.*registration.*administrator/);
  assert.match(communicationsDeliveryError({ code: "21610", source: "submission" }, "rejected"), /opted out.*stopped/);
  assert.match(communicationsDeliveryError({ code: "999999", source: "callback" }, "failed"), /Provider code 999999:.*administrator/);
});

test("missing delivery codes and pending or successful statuses do not imply failure", () => {
  for (const deliveryError of [undefined, null]) {
    const view = readCommunications(durable({ operations: [operation({ operationId: uuid, simulated: false,
      status: "undelivered", deliveryError })] }), "case-a");
    assert.equal(view.operations[0].deliveryError, null);
  }
  for (const status of ["prepared", "authorized", "queued", "sent", "delivered", "read", "completed"]) {
    assert.equal(communicationsDeliveryError({ code: "30034", source: "callback" }, status), null);
  }
  const issue = { code: "transport_uncertain", source: "submission" };
  const view = readCommunications(durable({ operations: [operation({ operationId: uuid, simulated: false,
    status: "unknown", deliveryError: issue, cancelable: false })] }), "case-a");
  assert.equal(view.operations[0].status, "unknown");
  assert.match(communicationsDeliveryError(issue, "unknown"), /could not be confirmed.*will not be sent again/);
});

test("untrusted delivery diagnostics cannot enter the rendered contract", () => {
  const read = (deliveryError, status = "failed") => readCommunications(durable({
    operations: [operation({ operationId: uuid, simulated: false, status, deliveryError })] }), "case-a");
  for (const issue of ["30034", {}, { code: 30034, source: "callback" },
    { code: "30034", source: "callback", message: "PRIVATE-CANARY" },
    { code: "30034", source: "untrusted" }, { code: "transport_uncertain", source: "callback" },
    ...["1234567", "30034\n", "30034\r", "PRIVATE-CANARY", "https://private.invalid", "__proto__"]
      .map((code) => ({ code, source: "callback" }))]) assert.throws(() => read(issue));
  assert.throws(() => read({ code: "30034", source: "callback" }, "delivered"));
});

test("SMS preparation and authorization are separate, bounded server decisions", () => {
  const value = durable();
  value.actions.sms.authorization = { allowed: false, reason: "communications_quiet_hours" };
  let view = readCommunications(value, "case-a");
  assert.equal(view.actions.sms.allowed, true);
  assert.deepEqual(view.actions.sms.authorization, value.actions.sms.authorization);
  assert.match(communicationsReason(view.actions.sms.authorization.reason), /quiet hours/);
  value.actions.sms.authorization = { allowed: true, reason: "" };
  assert.equal(readCommunications(value, "case-a").actions.sms.authorization.allowed, false);
  value.policy.outboundEnabled = true;
  view = readCommunications(value, "case-a");
  assert.equal(view.actions.sms.authorization.allowed, true);
  value.suppression.sms = true;
  value.actions.sms.allowed = false;
  value.actions.sms.reason = "communications_contact_suppressed";
  assert.equal(readCommunications(value, "case-a").actions.sms.authorization.allowed, false);
});

test("old or malformed authorization metadata preserves drafts but cannot grant send approval", () => {
  for (const auth of [undefined, null, {}, true, { allowed: "true", reason: "" },
    { allowed: true, reason: "communications_quiet_hours" }, { allowed: true, reason: "", extra: 1 }]) {
    const value = durable();
    value.actions.sms.authorization = auth;
    const view = readCommunications(value, "case-a");
    assert.equal(view.actions.sms.allowed, true);
    assert.equal(view.actions.sms.authorization.allowed, false);
    assert.match(communicationsReason(view.actions.sms.authorization.reason), /not reported/);
    assert.deepEqual(view.operations, []);
  }
});

test("enabled channels limit new actions without hiding historical calls or receipts", () => {
  const value = durable({ enabledChannels: ["sms"], operations: [operation({ operationId: uuid,
    channel: "automated_voice", simulated: false, status: "prepared", cancelable: true })],
    events: [event({ channel: "automated_voice", kind: "voice_gather", direction: "callback",
      text: null, disposition: "callback_requested" })] });
  const view = readCommunications(value, "case-a");
  assert.deepEqual(view.enabledChannels, ["sms"]);
  assert.equal(view.actions.sms.allowed, true);
  assert.equal(view.actions.automatedVoice.allowed, false);
  assert.equal(view.actions.automatedVoice.reason, "communications_channel_disabled");
  assert.equal(view.operations[0].cancelable, true);
  assert.equal(view.events[0].disposition, "callback_requested");
  assert.equal(readCommunications(durable(), "case-a").actions.automatedVoice.allowed, false);
  assert.equal(readCommunications(payload(), "case-a").actions.automatedVoice.allowed, false);
  const explicit = readCommunications({ ...value, enabledChannels: ["sms", "automated_voice"] }, "case-a");
  assert.equal(explicit.actions.automatedVoice.allowed, true);
  const readOnly = readCommunications({ ...value, enabledChannels: [] }, "case-a");
  assert.equal(readOnly.actions.sms.allowed, false);
  assert.equal(readOnly.operations.length, 1);
  view.enabledChannels.push("automated_voice");
  assert.deepEqual(value.enabledChannels, ["sms"]);
});

test("unknown or malformed enabled-channel contracts fail closed", () => {
  for (const enabledChannels of [null, "sms", ["sms", "sms"], ["voice"], ["sms", "whatsapp"],
    ["sms", "automated_voice", "email"], [true], { sms: true }]) {
    assert.throws(() => readCommunications(durable({ enabledChannels }), "case-a"));
  }
});

test("full event-review count is distinct from current history and unknown investigations", () => {
  const value = durable({ reviewPendingCount: 205, truncated: true, heldInboundCount: 2 });
  assert.equal(readCommunications(value, "case-a").reviewPendingCount, 205);
  for (const count of [NaN, -1, 1, "205"]) assert.throws(() => readCommunications({ ...value, reviewPendingCount: count }, "case-a"));
  const unknown = durable({ reviewPendingCount: 0, operations: [operation({ operationId: uuid, status: "unknown", simulated: false, needsStaffReview: true })] });
  assert.equal(readCommunications(unknown, "case-a").operations[0].needsStaffReview, true);
  assert.equal(readCommunications(unknown, "case-a").reviewPendingCount, 0);
  assert.equal(readCommunications(durable(), "case-a").reviewPendingCount, null);
});

test("durable projection permits selected-case verified contact and preserves held metadata", () => {
  const view = readCommunications(durable({ operations: [operation({ operationId: uuid, simulated: false,
    status: "blocked", blockedReason: "communications_quiet_hours", lastCheckedAt: at, providerSid: "private-sid",
    actorId: "private-actor" })] }), "case-a");
  assert.equal(view.contact.routes[0].destination, "+12025550100");
  assert.equal(view.bindings.sms.consentGranted, true);
  assert.equal(view.policy.outboundEnabled, false);
  assert.equal(view.operations[0].blockedReason, "communications_quiet_hours");
  assert.equal(JSON.stringify(view).includes("private"), false);
  assert.match(communicationsReason(view.operations[0].blockedReason), /quiet hours/);
  assert.match(communicationsStatus("prepared"), /not sent/);
});

test("durable view rejects unverified shape, invalid contact and mixed simulation data", () => {
  for (const change of [
    { policy: { hash: "a".repeat(64) } },
    { contact: { snapshotId: uuid, source: "synthetic", verifiedAt: at, routes: [{ channel: "sms", destination: "not-a-phone", consentStatus: "granted" }] } },
    { bindings: { sms: { bindingId: uuid, consentGranted: "true" } } },
    { operations: [operation({ operationId: uuid, status: "prepared", simulated: true })] },
    { operations: [operation({ operationId: uuid, status: "blocked", simulated: false, blockedReason: "communications_quiet_hours", lastCheckedAt: "bad" })] },
    { contact: null },
    { truncated: "false" },
  ]) assert.throws(() => readCommunications(durable(change), "case-a"));
});

test("durable attachment and staff events retain review without creating outbound bubbles", () => {
  const view = readCommunications(durable({ events: [
    event({ direction: "staff", kind: "staff_suppression", text: "", status: "suppressed" }),
    event({ eventId: "mms", text: "", mediaCount: 1, kind: "sms_inbound" }),
  ] }), "case-a");
  assert.equal(smsThread(view).length, 1);
  assert.equal(smsThread(view)[0].mediaCount, 1);
  assert.match(communicationError({ status: 503 }, "durable"), /same action/);
  assert.equal(communicationError({ status: 503 }, "durable").includes("simulation"), false);
  assert.equal(communicationsReason("UNKNOWN-PRIVATE").includes("PRIVATE"), false);
});

test("cancellation is offered only for an explicit unstarted state and keeps its time", () => {
  const read = (extra) => readCommunications(durable({ operations: [operation({
    operationId: uuid, simulated: false, status: "prepared", ...extra,
  })] }), "case-a");
  assert.equal(read({}).operations[0].cancelable, false);
  assert.equal(smsThread(read({ cancelable: true, canceledAt: null }))[0].cancelable, true);
  const canceled = read({ status: "canceled", cancelable: false, canceledAt: at });
  assert.equal(smsThread(canceled)[0].canceledAt, at);
  for (const extra of [{ cancelable: "true" }, { status: "unknown", cancelable: true },
    { status: "queued", cancelable: true }, { status: "canceled", cancelable: false, canceledAt: "bad" },
    { status: "delivered", canceledAt: at }]) assert.throws(() => read(extra));
});

test("investigation preserves unknown history and accepts only confirmed attributed results", () => {
  const read = (extra) => readCommunications(durable({ operations: [operation({ operationId: uuid,
    simulated: false, status: "unknown", canResolveUnknown: true, unknownResolution: null, ...extra })] }), "case-a");
  assert.equal(smsThread(read({}))[0].canResolveUnknown, true);
  const resolution = { decision: "confirmed_attempted", vendorReference: "synthetic:review-1", resolvedAt: at };
  const reviewed = read({ canResolveUnknown: false, unknownResolution: resolution });
  assert.equal(reviewed.operations[0].status, "unknown");
  assert.deepEqual(smsThread(reviewed)[0].unknownResolution, resolution);
  for (const extra of [{ status: "delivered" }, { canResolveUnknown: "true" },
    { unknownResolution: resolution },
    { canResolveUnknown: false, unknownResolution: { ...resolution, decision: "probably_not_sent" } },
    { canResolveUnknown: false, unknownResolution: { ...resolution, vendorReference: "" } },
    { canResolveUnknown: false, unknownResolution: { ...resolution, resolvedAt: "bad" } }]) assert.throws(() => read(extra));
});
