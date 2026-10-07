import { expect, test, workspaceRows } from "./sms-workspace.fixture.js";

const basePort = Number.parseInt(process.env.COSMANEURA_E2E_BASE_PORT || "8765", 10);
const origin = "http://127.0.0.1:" + basePort;
const base = "/api/chase/communications";
const at = "2026-09-29T16:00:00.123456+00:00";
const snapshot = "11111111-1111-4111-8111-111111111111";
const binding = "22222222-2222-4222-8222-222222222222";
const smsAvailability = (allowed, reason = "") => ({ allowed, reason, authorization: { allowed, reason } });

function payload(chaseId = "") {
  return { schemaVersion: "chase-communications.v1", chaseId, mode: "durable", truncated: false,
    enabledChannels: ["sms", "automated_voice"],
    operations: [], events: [], suppression: { sms: false, automatedVoice: false }, heldInboundCount: 0,
    actions: { sms: smsAvailability(false, "communications_policy_approval_required"),
      automatedVoice: { allowed: false, reason: "communications_purpose_consent_required" } },
    contact: { snapshotId: snapshot, source: "synthetic-staff-review", verifiedAt: at,
      routes: [{ channel: "sms", destination: "+12025550100", consentStatus: "unknown" },
        { channel: "automated_voice", destination: "+12025550100", consentStatus: "unknown" }] },
    bindings: {}, policy: { hash: "a".repeat(64), approved: false, canApprove: true, outboundEnabled: true,
      practiceName: "Synthetic Practice", timezone: "UTC", quietHours: [21, 8], maxTotalTouches: 8, maxTouches24h: 2 },
    templates: [{ id: "reminder", label: "Reminder", text: "Synthetic Practice: Please contact our office. Reply STOP to stop." }],
    callScript: "Synthetic Practice: Press 1 to request a callback, 2 to report submitted, or 9 to stop calls." };
}

async function open(page) {
  await page.goto(origin);
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Card chase", exact: true }).click();
  await page.getByRole("button", { name: /Synthetic patient A/ }).click();
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  const panel = page.getByRole("region", { name: "Case communications", exact: true });
  await expect(panel.getByRole("heading", { name: "Text conversation" })).toBeVisible();
  return panel;
}

test("delivery errors show a safe reason and clear after success without enabling a resend", async ({ page }) => {
  const view = payload(), mutations = [];
  await page.route("**/api/chase/communications**", async (route) => {
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (path === "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    if (path === "/review-events") return route.fulfill({ json: {
      schemaVersion: "communications-review.v1", chaseId: body.chaseId,
      totalPending: 0, heldPending: 0, truncated: false, items: [],
    } });
    if (path) mutations.push(path);
    if (!view.chaseId) {
      view.chaseId = body.chaseId;
      view.operations.push({ operationId: "33333333-3333-4333-8333-333333333333", caseId: view.chaseId,
        channel: "sms", direction: "outbound", text: "Synthetic Practice: Please contact our office. Reply STOP to stop.",
        status: "undelivered", simulated: false, createdAt: at, needsStaffReview: true, cancelable: false,
        deliveryError: { code: "30034", source: "callback" }, providerMessage: "PRIVATE-CANARY" });
    }
    return route.fulfill({ json: view });
  });
  const panel = await open(page);
  await expect(panel.getByLabel("Delivery problem", { exact: true })).toContainText("Provider code 30034");
  await expect(panel.getByLabel("Delivery problem", { exact: true })).toContainText("A2P registration");
  await expect(panel).not.toContainText("PRIVATE-CANARY");
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Cancel SMS", exact: true })).toHaveCount(0);
  Object.assign(view.operations[0], { status: "delivered", deliveryError: null, needsStaffReview: false });
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByLabel("Delivery problem", { exact: true })).toHaveCount(0);
  Object.assign(view.operations[0], { status: "unknown", needsStaffReview: true,
    deliveryError: { code: "transport_uncertain", source: "submission" }, canResolveUnknown: false });
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByLabel("Delivery problem", { exact: true })).toContainText("will not be sent again");
  expect(mutations).toEqual([]);
});

test("texting-only workspaces keep call history and cancellation without offering new calls", async ({ page }) => {
  const view = payload();
  view.enabledChannels = ["sms"];
  view.policy.approved = true;
  view.actions.sms = { allowed: true, reason: "" };
  // Even an inconsistent old action flag cannot enable a disabled channel.
  view.actions.automatedVoice = { allowed: true, reason: "" };
  const callId = "33333333-3333-4333-8333-333333333333";
  const mutations = [];
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (path === "/review-events") return route.fulfill({ json: {
      schemaVersion: "communications-review.v1", chaseId: body.chaseId,
      totalPending: 0, heldPending: 0, truncated: false, items: [],
    } });
    if (!view.chaseId) {
      view.chaseId = body.chaseId;
      view.operations.push({ operationId: callId, caseId: view.chaseId,
        channel: "automated_voice", direction: "outbound", text: "Previously prepared synthetic call script.",
        status: "prepared", simulated: false, createdAt: at, needsStaffReview: true,
        cancelable: true });
    }
    if (path) mutations.push({ path, body });
    if (path === "/cancel") {
      expect(body.operationId).toBe(callId);
      expect(body.confirmed).toBe(true);
      Object.assign(view.operations[0], { status: "canceled", cancelable: false, canceledAt: at });
    }
    await route.fulfill({ json: view });
  });
  const panel = await open(page);
  await expect(panel.getByRole("textbox", { name: "SMS draft", exact: true })).toBeEnabled();
  await expect(panel.getByRole("region", { name: "Automated calls contact and permission" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Prepare scripted call", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Authorize call", exact: true })).toHaveCount(0);
  const history = panel.getByRole("region", { name: "Call history", exact: true });
  await expect(history).toContainText("Previously prepared synthetic call script.");
  await history.getByRole("button", { name: "Cancel call", exact: true }).click();
  await expect(history).toContainText("Canceled before dispatch");
  expect(mutations.map((r) => r.path)).toEqual(["/cancel"]);
  view.enabledChannels = ["sms", "automated_voice"];
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Prepare scripted call", exact: true })).toBeVisible();
  await expect(panel.getByRole("region", { name: "Automated calls contact and permission" })).toBeVisible();
});

test("staff verify contact and permission, prepare and explicitly authorize; no demo sends", async ({ page }) => {
  const view = payload();
  const requests = [];
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    requests.push({ path, body });
    if (!view.chaseId && body.chaseId) view.chaseId = body.chaseId;
    if (path === "/policy/decision") {
      expect(Object.keys(body).sort()).toEqual(["actionId", "approved", "expectedPolicyHash"]);
      expect(body.expectedPolicyHash).toBe(view.policy.hash);
      view.policy.approved = body.approved;
      return route.fulfill({ json: { approved: body.approved, policyHash: view.policy.hash, decisionId: body.actionId } });
    }
    if (path === "/bind") {
      expect(Object.keys(body).sort()).toEqual(["channel", "chaseId", "confirmedContact", "contactSnapshotId"]);
      expect(body.confirmedContact).toBe(true);
      expect(body.contactSnapshotId).toBe(snapshot);
      view.bindings[body.channel] = { bindingId: binding, consentGranted: false };
      view.actions.sms.reason = "communications_purpose_consent_required";
    }
    if (path === "/consent") {
      expect(body.confirmed).toBe(true);
      expect(body.sourceReference).toBe("synthetic-consent:reviewed");
      expect(body.bindingId).toBe(binding);
      view.bindings.sms.consentGranted = body.granted;
      view.actions.sms = smsAvailability(body.granted && view.policy.approved);
    }
    if (path === "/action") {
      expect(view.bindings.sms.consentGranted).toBe(true);
      view.operations.push({ operationId: body.actionId, caseId: view.chaseId, channel: "sms",
        direction: "outbound", text: body.text, status: "prepared", simulated: false, createdAt: at, needsStaffReview: true });
    }
    if (path === "/authorize") {
      expect(Object.keys(body).sort()).toEqual(["chaseId", "confirmed", "operationId"]);
      expect(body.confirmed).toBe(true);
      view.operations.find((o) => o.operationId === body.operationId).status = "authorized";
    }
    await route.fulfill({ json: view });
  });
  const panel = await open(page);
  await expect(panel.getByRole("button", { name: "Prepare SMS", exact: true })).toBeDisabled();
  await expect(panel.getByText("Demo events", { exact: true })).toHaveCount(0);
  await panel.locator("summary").filter({ hasText: "Outreach policy" }).click();
  await panel.getByLabel("I reviewed the current templates, script, limits, and quiet hours.").check();
  await panel.getByRole("button", { name: "Approve outreach policy", exact: true }).click();
  const contact = panel.getByRole("region", { name: "Text messages contact and permission" });
  await contact.getByLabel("I verified this contact against the current source.").check();
  await contact.getByRole("button", { name: "Verify text contact", exact: true }).click();
  await contact.locator("summary").filter({ hasText: "Record or update permission" }).click();
  await contact.getByLabel("Permission for text messages", { exact: true }).selectOption("granted");
  await contact.getByLabel("Text messages consent record reference", { exact: true }).fill("synthetic-consent:reviewed");
  await contact.getByLabel("I reviewed explicit permission for insurance follow-up by text messages.").check();
  await contact.getByRole("button", { name: "Record text permission", exact: true }).click();
  await panel.getByRole("button", { name: "Reminder", exact: true }).click();
  await panel.getByRole("button", { name: "Prepare SMS", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Prepared · not sent");
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toBeDisabled();
  await panel.getByLabel("I reviewed this exact message, recipient, and permission.").check();
  await panel.getByRole("button", { name: "Authorize SMS", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Authorized · awaiting dispatch");
  expect(requests.filter((r) => r.path === "/action")).toHaveLength(1);
  expect(requests.filter((r) => r.path === "/authorize")).toHaveLength(1);
  view.operations[0].status = "delivered";
  view.events.push({ eventId: "reply", kind: "sms_inbound", direction: "inbound", channel: "sms",
    text: "Synthetic reply: please call.", status: "received", disposition: "reply_received", occurredAt: at, needsStaffReview: true });
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Delivered");
  await expect(panel).toContainText("Synthetic reply: please call.");
  view.suppression.sms = true;
  view.actions.sms = { allowed: false, reason: "communications_contact_suppressed" };
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel).toContainText("SMS suppressed");
  await expect(panel.getByRole("button", { name: "Prepare SMS", exact: true })).toBeDisabled();
});

test("an uncertain prepare retries its same ID; stale case reads cannot replace the current case", async ({ page }) => {
  await page.route("**/api/session", (route) => route.fulfill({ json: {
    views: ["provider", "coder", "chase"], defaultView: "chase",
    capabilities: { manageChase: true },
    actions: { seedSyntheticChase: { seedDigest: "b".repeat(64), caseCount: 4 } },
  } }));
  await page.route("**/api/encounters", (route) => route.fulfill({ json: [] }));
  const view = payload();
  view.policy.approved = true;
  view.bindings.sms = { bindingId: binding, consentGranted: true };
  view.actions.sms = { allowed: true, reason: "" };
  const bodies = [];
  let delayed = false, started, release;
  const began = new Promise((resolve) => { started = resolve; });
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!view.chaseId) view.chaseId = body.chaseId;
    if (path === "/action") {
      bodies.push(body);
      if (bodies.length === 1) {
        view.operations.push({ operationId: body.actionId, caseId: view.chaseId, channel: "sms", direction: "outbound",
          text: body.text, status: "prepared", simulated: false, createdAt: at, needsStaffReview: true });
        return route.fulfill({ status: 503, json: {} });
      }
    }
    if (body.chaseId !== view.chaseId) {
      const other = payload(body.chaseId);
      other.contact = null;
      return route.fulfill({ json: other });
    }
    if (!path && delayed) { started(); await gate; }
    await route.fulfill({ json: view }).catch(() => {});
  });
  const panel = await open(page);
  await panel.getByRole("button", { name: "Reminder", exact: true }).click();
  await panel.getByRole("button", { name: "Prepare SMS", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Retry same action", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeDisabled();
  // The exact pending command is owned by this case component. Navigation
  // must preserve it until its outcome is confirmed by the same-action retry.
  await expect(page.getByRole("button", { name: /Synthetic patient B/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: /^Expired/ })).toBeDisabled();
  await page.getByText("Demo setup", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Load synthetic demo tasks", exact: true })).toBeDisabled();
  await expect(page.getByRole("tab", { name: "Summary", exact: true })).toBeDisabled();
  await expect(page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Provider", exact: true })).toBeDisabled();
  await expect(page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Coder review", exact: true })).toBeDisabled();
  await expect(page.getByRole("status").filter({ hasText: "A communications action is pending" })).toBeVisible();
  await panel.getByRole("button", { name: "Retry same action", exact: true }).click();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: /Synthetic patient B/ })).toBeEnabled();
  await expect(page.getByRole("button", { name: /^Expired/ })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Load synthetic demo tasks", exact: true })).toBeEnabled();
  await expect(page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Provider", exact: true })).toBeEnabled();
  await expect(panel.getByRole("list", { name: "SMS thread" }).getByRole("listitem")).toHaveCount(1);
  expect(bodies).toHaveLength(2);
  expect(bodies[0]).toEqual(bodies[1]);
  delayed = true;
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await began;
  await page.getByRole("button", { name: /Synthetic patient B/ }).click();
  release();
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  await expect(panel).toContainText("A staff-reviewed contact source is needed");
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("No recorded messages yet");
  await expect(panel.getByRole("list", { name: "SMS thread" })).not.toContainText(view.operations[0].text);
  await expect(panel.getByText("+12025550100", { exact: true })).toHaveCount(0);
});

test("a pending draft can be canceled; a started uncertain send cannot be recalled", async ({ page }) => {
  const view = payload();
  view.policy.approved = true;
  view.bindings.sms = { bindingId: binding, consentGranted: true };
  view.actions.sms = { allowed: true, reason: "" };
  let cancellations = 0;
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!view.chaseId) view.chaseId = body.chaseId;
    if (path === "/action") view.operations.push({ operationId: body.actionId, caseId: view.chaseId,
      channel: "sms", direction: "outbound", text: body.text, status: "prepared", simulated: false,
      createdAt: at, needsStaffReview: true, cancelable: true, canceledAt: null });
    if (path === "/cancel") {
      expect(Object.keys(body).sort()).toEqual(["chaseId", "confirmed", "operationId"]);
      expect(body.operationId).toBe(view.operations[0].operationId);
      expect(body.confirmed).toBe(true);
      Object.assign(view.operations[0], { status: "canceled", cancelable: false, canceledAt: at });
      cancellations++;
    }
    await route.fulfill({ json: view });
  });
  const panel = await open(page);
  await panel.getByRole("button", { name: "Reminder", exact: true }).click();
  await panel.getByRole("button", { name: "Prepare SMS", exact: true }).click();
  await panel.getByRole("button", { name: "Cancel SMS", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Canceled before dispatch");
  await expect(panel.getByRole("button", { name: "Cancel SMS", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toHaveCount(0);
  expect(cancellations).toBe(1);
  Object.assign(view.operations[0], { status: "unknown", cancelable: false, canceledAt: null });
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Outcome unknown · review required");
  await expect(panel.getByRole("button", { name: "Cancel SMS", exact: true })).toHaveCount(0);
});
test("timed-out prepare and authorization release busy state and retry the exact command", async ({ page }) => {
  await page.clock.install();
  const view = payload();
  view.policy.approved = true;
  view.bindings.sms = { bindingId: binding, consentGranted: true };
  view.actions.sms = smsAvailability(true);
  const bodies = { "/action": [], "/authorize": [] };
  const seen = {}, released = {}, began = {}, gates = {};
  for (const path of Object.keys(bodies)) {
    began[path] = new Promise((resolve) => { seen[path] = resolve; });
    gates[path] = new Promise((resolve) => { released[path] = resolve; });
  }
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!view.chaseId) view.chaseId = body.chaseId;
    if (bodies[path]) {
      bodies[path].push(body);
      if (bodies[path].length === 1) {
        if (path === "/action") view.operations.push({ operationId: body.actionId, caseId: view.chaseId,
          channel: "sms", direction: "outbound", text: body.text, status: "prepared", simulated: false,
          createdAt: at, needsStaffReview: true, cancelable: true, canceledAt: null });
        else view.operations[0].status = "authorized";
        seen[path]();
        await gates[path];
      }
    }
    await route.fulfill({ json: view }).catch(() => {});
  });
  const panel = await open(page);
  await panel.getByRole("button", { name: "Reminder", exact: true }).click();
  await panel.getByRole("button", { name: "Prepare SMS", exact: true }).click();
  await began["/action"];
  await page.clock.fastForward(15_001);
  const retry = panel.getByRole("button", { name: "Retry same action", exact: true });
  await expect(retry).toBeEnabled();
  await retry.click();
  released["/action"]();
  await expect(panel.getByRole("list", { name: "SMS thread" }).getByRole("listitem")).toHaveCount(1);
  expect(bodies["/action"]).toHaveLength(2);
  expect(bodies["/action"][0]).toEqual(bodies["/action"][1]);
  await panel.getByLabel("I reviewed this exact message, recipient, and permission.").check();
  await panel.getByRole("button", { name: "Authorize SMS", exact: true }).click();
  await began["/authorize"];
  await page.clock.fastForward(15_001);
  await expect(retry).toBeEnabled();
  await retry.click();
  released["/authorize"]();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Authorized · awaiting dispatch");
  expect(bodies["/authorize"]).toHaveLength(2);
  expect(bodies["/authorize"][0]).toEqual(bodies["/authorize"][1]);
  expect(view.operations).toHaveLength(1);
});

test("SMS drafts save and reload while dispatch is paused, with separate live authorization", async ({ page }) => {
  const view = payload();
  view.enabledChannels = ["sms"];
  view.policy.approved = true;
  view.policy.outboundEnabled = false;
  view.bindings.sms = { bindingId: binding, consentGranted: true };
  view.actions.sms = { allowed: true, reason: "",
    authorization: { allowed: false, reason: "communications_dispatch_disabled" } };
  const mutations = [];
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (path === "/review-events") return route.fulfill({ json: {
      schemaVersion: "communications-review.v1", chaseId: body.chaseId,
      totalPending: 0, heldPending: 0, truncated: false, items: [],
    } });
    if (!view.chaseId) view.chaseId = body.chaseId;
    if (path) mutations.push({ path, body });
    if (path === "/action") view.operations.push({ operationId: body.actionId, caseId: view.chaseId,
      channel: "sms", direction: "outbound", text: body.text, status: "prepared", simulated: false,
      createdAt: at, needsStaffReview: false, cancelable: true });
    if (path === "/authorize") {
      expect(view.actions.sms.authorization.allowed).toBe(true);
      expect(body.operationId).toBe(view.operations[0].operationId);
      view.operations[0].status = "authorized";
    }
    await route.fulfill({ json: view });
  });
  let panel = await open(page);
  const text = "Synthetic Practice: Please contact our office about our request. Reply STOP to stop.";
  await panel.getByRole("textbox", { name: "SMS draft", exact: true }).fill(text);
  await panel.getByRole("button", { name: "Prepare SMS", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Prepared · not sent");
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toBeDisabled();
  await expect(panel.getByText(/This draft is saved; nothing has been sent/)).toBeVisible();
  panel = await open(page);
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText(text);
  expect(mutations.map((r) => r.path)).toEqual(["/action"]);
  const draft = panel.getByRole("textbox", { name: "SMS draft", exact: true });
  await draft.fill("Unfinished additional staff draft.");
  view.policy.outboundEnabled = true;
  view.actions.sms.authorization = { allowed: false, reason: "communications_quiet_hours" };
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByText(/Held during the practice's quiet hours/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toBeDisabled();
  await expect(draft).toHaveValue("Unfinished additional staff draft.");
  delete view.actions.sms.authorization;
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByText(/Authorization availability was not reported/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toBeDisabled();
  view.actions.sms = smsAvailability(true);
  await panel.getByRole("button", { name: "Refresh communications", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Authorize SMS", exact: true })).toBeDisabled();
  await panel.getByLabel("I reviewed this exact message, recipient, and permission.").check();
  await panel.getByRole("button", { name: "Authorize SMS", exact: true }).click();
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Authorized · awaiting dispatch");
  expect(mutations.map((r) => r.path)).toEqual(["/action", "/authorize"]);
});
test("unknown outcome holds both channels until an explicit recorded investigation without resending", async ({ page }) => {
  const view = payload();
  view.policy.approved = true;
  view.bindings = { sms: { bindingId: binding, consentGranted: true }, automated_voice: { bindingId: binding, consentGranted: true } };
  view.actions.sms = view.actions.automatedVoice = { allowed: false, reason: "communications_unknown_requires_resolution" };
  const operationId = "33333333-3333-4333-8333-333333333333";
  view.operations.push({ operationId, caseId: "", channel: "sms", direction: "outbound",
    text: "Synthetic Practice: Please contact our office. Reply STOP to stop.", status: "unknown", simulated: false,
    createdAt: at, needsStaffReview: true, cancelable: false, canceledAt: null,
    canResolveUnknown: true, unknownResolution: null });
  const requests = [];
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    requests.push(path);
    if (!view.chaseId) { view.chaseId = body.chaseId; view.operations[0].caseId = body.chaseId; }
    if (path === "/resolve-unknown") {
      expect(body).toEqual({ chaseId: view.chaseId, operationId, decision: "confirmed_attempted",
        vendorReference: "synthetic-vendor:review-1", confirmed: true });
      Object.assign(view.operations[0], { canResolveUnknown: false, needsStaffReview: false,
        unknownResolution: { decision: body.decision, vendorReference: body.vendorReference, resolvedAt: at } });
      view.actions.sms = view.actions.automatedVoice = { allowed: true, reason: "" };
    }
    await route.fulfill({ json: view });
  });
  const panel = await open(page);
  await expect(panel.getByRole("button", { name: "Prepare SMS", exact: true })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Prepare scripted call", exact: true })).toBeDisabled();
  await panel.locator("summary").filter({ hasText: "Resolve unknown outcome" }).click();
  const record = panel.getByRole("button", { name: "Record investigated outcome", exact: true });
  await expect(record).toBeDisabled();
  await panel.getByLabel("SMS investigated outcome", { exact: true }).selectOption("confirmed_attempted");
  await panel.getByLabel("SMS investigation reference", { exact: true }).fill("synthetic-vendor:review-1");
  await expect(record).toBeDisabled();
  await panel.getByLabel("I confirmed this outcome in the provider's records.", { exact: true }).check();
  await record.click();
  await expect(panel).toContainText("Investigation recorded · Attempt confirmed · delivery not established");
  await expect(panel.getByRole("list", { name: "SMS thread" })).toContainText("Original outcome unknown · investigated");
  await expect(panel.getByRole("button", { name: "Prepare scripted call", exact: true })).toBeEnabled();
  expect(requests.filter((path) => path === "/resolve-unknown")).toHaveLength(1);
  expect(requests.filter((path) => path === "/action")).toHaveLength(0);
});


test("communications stay hidden unless the server advertises a supported capability", async ({ page }) => {
  const unexpected = [];
  await page.route("**/api/chase/communications**", (route) => { unexpected.push(route.request().url()); return route.abort(); });
  for (const capability of [null, { available: false, mode: "durable" }, { available: true, mode: "unknown" }]) {
    await page.route("**/api/chase/task", (route) => route.fulfill({ json: {
      ...workspaceRows[0], open: true, assigned: true, allowedActions: [],
      systemOfRecord: { name: "Synthetic training", instruction: "Review locally." },
      communications: capability,
    } }));
    await page.goto(origin);
    await page.getByRole("button", { name: /Synthetic patient A/ }).click();
    await expect(page.getByRole("tab", { name: "Summary", exact: true })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Communications", exact: true })).toHaveCount(0);
  }
  expect(unexpected).toEqual([]);
});
