import { expect, test, workspaceRows } from "./sms-workspace.fixture.js";

const origin = `http://127.0.0.1:${Number.parseInt(process.env.COSMANEURA_E2E_BASE_PORT || "8765", 10)}`;
const base = "/api/chase/communications";
const at = "2026-09-29T16:00:00.123456+00:00";
const snapshot = "11111111-1111-4111-8111-111111111111";
function communication(chaseId) {
  return { schemaVersion: "chase-communications.v1", chaseId, mode: "durable", truncated: false,
    enabledChannels: ["sms", "automated_voice"],
    operations: [], events: [], suppression: { sms: true, automatedVoice: true }, heldInboundCount: 0,
    actions: { sms: { allowed: false, reason: "communications_contact_suppressed" },
      automatedVoice: { allowed: false, reason: "communications_unknown_requires_resolution" } },
    contact: null, bindings: {}, policy: { hash: "a".repeat(64), approved: false, canApprove: true, outboundEnabled: false,
      practiceName: "Synthetic Practice", timezone: "UTC", quietHours: [21, 8], maxTotalTouches: 8, maxTouches24h: 2 },
    templates: [], callScript: "Synthetic practice: Press 1 for a callback, 2 if submitted, or 9 to stop calls." };
}
const item = (digit = "a", extra = {}) => ({ ref: digit.repeat(64), sourceHash: "f".repeat(64), kind: "sms_reply", channel: "sms",
  text: `Synthetic message ${digit}`, at, status: "received", disposition: "reply_received", held: false,
  canAttribute: false, contactSnapshotId: null, reviewState: "pending", mediaCount: 0, ...extra });
const review = (chaseId, items) => ({ schemaVersion: "communications-review.v1", chaseId,
  totalPending: items.length, heldPending: items.filter((entry) => entry.held).length, items, truncated: false });

async function open(page) {
  await page.goto(origin);
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Card chase", exact: true }).click();
  await page.getByRole("button", { name: /Synthetic patient A/ }).click();
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  const panel = page.getByRole("region", { name: "Case communications", exact: true });
  await expect(panel.getByRole("heading", { name: "Text conversation" })).toBeVisible();
  await panel.locator("summary").filter({ hasText: /^Staff review/ }).click();
  const staff = panel.getByRole("region", { name: "Communication event review", exact: true });
  await expect(staff.getByRole("list", { name: "Events awaiting staff review" })).toBeVisible();
  return { panel, staff };
}
async function choose(row, decision) {
  await row.getByLabel("Review decision", { exact: true }).selectOption(decision);
  await row.getByRole("checkbox").check();
  await row.getByRole("button", { name: "Save review", exact: true }).click();
}

test("held attribution is explicit and still pending; handling refreshes history and counts without restoring permission", async ({ page }) => {
  let items = [item(), item("b", { held: true, canAttribute: true, contactSnapshotId: snapshot, mediaCount: 2 }),
    item("c", { held: true })];
  const commands = []; let historyReads = 0, queueReads = 0;
  await page.route("**/api/chase/worklist", async (route) => {
    queueReads++;
    const value = { rows: structuredClone(workspaceRows) };
    value.rows = value.rows.map((row) => ({ ...row, communicationReview: {
      pending: row.patientLabel === "Synthetic patient A" ? items.length : row.patientLabel === "Synthetic patient B" ? items.filter((entry) => entry.held).length : 0,
      held: ["Synthetic patient A", "Synthetic patient B"].includes(row.patientLabel) ? items.filter((entry) => entry.held).length : 0,
    } }));
    await route.fulfill({ json: value });
  });
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!path) { historyReads++; return route.fulfill({ json: communication(body.chaseId) }); }
    if (path === "/review-event") {
      commands.push(body);
      expect(Object.keys(body).sort()).toEqual(["actionId", "chaseId", "confirmed", "contactSnapshotId", "decision", "eventRef", "sourceHash"]);
      expect(body.confirmed).toBe(true); expect(body.sourceHash).toBe("f".repeat(64));
      if (body.decision === "attribute") {
        expect(body.contactSnapshotId).toBe(snapshot);
        items = items.map((entry) => entry.ref === body.eventRef ? { ...entry, held: false, canAttribute: false, contactSnapshotId: null, reviewState: "attributed" } : entry);
      } else { expect(body.contactSnapshotId).toBeNull(); items = items.filter((entry) => entry.ref !== body.eventRef); }
    } else expect(path).toBe("/review-events");
    await route.fulfill({ json: review(body.chaseId, items) });
  });
  const { panel, staff } = await open(page);
  const row = (text) => staff.getByRole("listitem").filter({ hasText: text });
  await expect(page.getByRole("button", { name: /Synthetic patient A/ })).toContainText("3 communication events to review");
  for (const select of await staff.getByLabel("Review decision", { exact: true }).all()) await expect(select).toHaveValue("");
  for (const button of await staff.getByRole("button", { name: "Save review", exact: true }).all()) await expect(button).toBeDisabled();
  await expect(row("Synthetic message b")).toContainText("2 attachments reported");
  await expect(row("Synthetic message b").getByRole("option", { name: "Mark handled", exact: true })).toHaveCount(0);
  await expect(row("Synthetic message c").getByRole("option", { name: "Attribute to this case", exact: true })).toHaveCount(0);
  await row("Synthetic message b").getByLabel("Review decision", { exact: true }).selectOption("attribute");
  await expect(row("Synthetic message b").getByRole("button", { name: "Save review", exact: true })).toBeDisabled();
  await row("Synthetic message b").getByLabel("I verified that this message belongs to this case and matches its current contact.").check();
  await row("Synthetic message b").getByRole("button", { name: "Save review", exact: true }).click();
  await expect(row("Synthetic message b")).toContainText("Attributed by staff · Follow-up still pending");
  await expect(staff).toContainText("3 pending · 1 held");
  await expect(page.getByRole("button", { name: /Synthetic patient B/ })).toContainText("1 communication event to review");
  await choose(row("Synthetic message b"), "handled");
  await expect(row("Synthetic message b")).toHaveCount(0);
  await choose(row("Synthetic message c"), "leave_unassigned");
  await expect(row("Synthetic message c")).toHaveCount(0);
  await choose(row("Synthetic message a"), "handled");
  await expect(staff).toContainText("No communication events await staff review.");
  await expect(page.getByRole("button", { name: /Synthetic patient A/ })).not.toContainText("communication events to review");
  await expect(panel).toContainText("SMS suppressed · STOP");
  await expect(panel).toContainText("Investigate the earlier unknown outcome");
  expect(commands.map((entry) => entry.decision)).toEqual(["attribute", "handled", "leave_unassigned", "handled"]);
  expect(historyReads).toBeGreaterThanOrEqual(5);
  expect(queueReads).toBeGreaterThanOrEqual(5);
});

test("an uncertain save preserves exact identity and choices while disabling refresh and outreach", async ({ page }) => {
  const commands = []; let items = [item()];
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!path) return route.fulfill({ json: communication(body.chaseId) });
    if (path === "/review-event") {
      commands.push(body); items = [];
      if (commands.length === 1) return route.fulfill({ status: 503, json: {} });
    }
    await route.fulfill({ json: review(body.chaseId, items) });
  });
  const { panel, staff } = await open(page);
  await choose(staff.getByRole("listitem"), "handled");
  await expect(staff.getByRole("alert")).toContainText("could not be confirmed");
  await expect(staff.getByLabel("Review decision", { exact: true })).toBeDisabled();
  await expect(staff.getByRole("button", { name: "Refresh staff review", exact: true })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Refresh communications", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeDisabled();
  await staff.getByRole("button", { name: "Retry same review", exact: true }).click();
  await expect(staff).toContainText("No communication events await staff review.");
  expect(commands).toHaveLength(2); expect(commands[0]).toEqual(commands[1]);
});

test("a review deadline releases busy state and retries the exact decision", async ({ page }) => {
  await page.clock.install();
  const commands = []; let started, release;
  const began = new Promise((resolve) => { started = resolve; });
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!path) return route.fulfill({ json: communication(body.chaseId) });
    if (path === "/review-event") {
      commands.push(body);
      if (commands.length === 1) { started(); await gate; }
    }
    await route.fulfill({ json: review(body.chaseId, commands.length ? [] : [item()]) }).catch(() => {});
  });
  const { staff } = await open(page);
  await choose(staff.getByRole("listitem"), "handled");
  await began;
  await page.clock.runFor(15_001);
  await expect(staff.getByRole("button", { name: "Retry same review", exact: true })).toBeEnabled();
  release();
  await staff.getByRole("button", { name: "Retry same review", exact: true }).click();
  await expect(staff).toContainText("No communication events await staff review.");
  expect(commands).toHaveLength(2); expect(commands[0]).toEqual(commands[1]);
});

test("changed source conflicts force fresh review and never reuse the old attestation", async ({ page }) => {
  let changed = false;
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    if (!path) return route.fulfill({ json: communication(body.chaseId) });
    if (path === "/review-event") { changed = true; return route.fulfill({ status: 409, json: {} }); }
    await route.fulfill({ json: review(body.chaseId, [item("a", { sourceHash: (changed ? "e" : "f").repeat(64) })]) });
  });
  const { staff } = await open(page);
  await choose(staff.getByRole("listitem"), "handled");
  await expect(staff.getByRole("alert")).toContainText("event or contact changed");
  await expect(staff.getByRole("button", { name: "Retry same review", exact: true })).toHaveCount(0);
  await expect(staff.getByLabel("Review decision", { exact: true })).toBeDisabled();
  await staff.getByRole("button", { name: "Refresh staff review", exact: true }).click();
  await expect(staff.getByLabel("Review decision", { exact: true })).toHaveValue("");
  await expect(staff.getByRole("button", { name: "Save review", exact: true })).toBeDisabled();
});

test("late case responses cannot display or mutate the newly selected case", async ({ page }) => {
  let firstCase, wait = false, started, release;
  const began = new Promise((resolve) => { started = resolve; });
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    firstCase ||= body.chaseId;
    if (!path) return route.fulfill({ json: communication(body.chaseId) });
    if (wait && body.chaseId === firstCase) { started(); await gate; }
    await route.fulfill({ json: review(body.chaseId, body.chaseId === firstCase ? [item()] : []) }).catch(() => {});
  });
  const { staff } = await open(page);
  wait = true;
  await staff.getByRole("button", { name: "Refresh staff review", exact: true }).click();
  await began;
  await page.getByRole("button", { name: /Synthetic patient B/ }).click();
  release();
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  const panel = page.getByRole("region", { name: "Case communications", exact: true });
  await panel.locator("summary").filter({ hasText: /^Staff review/ }).click();
  await expect(panel).toContainText("No communication events await staff review.");
  await expect(panel).not.toContainText("Synthetic message a");
  await expect(page.getByRole("button", { name: /Synthetic patient B/ })).not.toContainText("communication events to review");
});

test("unverified cross-case review data never renders an event or a zero-pending claim", async ({ page }) => {
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    await route.fulfill({ json: path ? review("wrong-case", [item()]) : communication(body.chaseId) });
  });
  await page.goto(origin);
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Card chase", exact: true }).click();
  await page.getByRole("button", { name: /Synthetic patient A/ }).click();
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  const panel = page.getByRole("region", { name: "Case communications", exact: true });
  await panel.locator("summary").filter({ hasText: /^Staff review/ }).click();
  await expect(panel.getByRole("alert")).toContainText("Staff review could not be loaded");
  await expect(panel).not.toContainText("Synthetic message a");
  await expect(panel).not.toContainText("No communication events await staff review.");
});

test("a late save response from another case cannot overwrite counts or show its success", async ({ page }) => {
  let firstCase, started, release;
  const began = new Promise((resolve) => { started = resolve; });
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/chase/communications**", async (route) => {
    if (new URL(route.request().url()).pathname === base + "/unassigned-texts/count") return route.fulfill({ status: 404, json: {} });
    const body = route.request().postDataJSON();
    const path = new URL(route.request().url()).pathname.slice(base.length);
    firstCase ||= body.chaseId;
    if (!path) return route.fulfill({ json: communication(body.chaseId) });
    if (path === "/review-event") { started(); await gate; }
    await route.fulfill({ json: review(body.chaseId, body.chaseId === firstCase ? [item()] : [item("b")]) }).catch(() => {});
  });
  const { staff } = await open(page);
  await choose(staff.getByRole("listitem"), "handled");
  await began;
  await page.getByRole("button", { name: /Synthetic patient B/ }).click();
  release();
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  const panel = page.getByRole("region", { name: "Case communications", exact: true });
  await panel.locator("summary").filter({ hasText: /^Staff review/ }).click();
  await expect(panel).toContainText("Synthetic message b");
  await expect(panel).not.toContainText("Synthetic message a");
  await expect(panel).not.toContainText("Staff review recorded.");
  await expect(page.getByRole("button", { name: /Synthetic patient B/ })).toContainText("1 communication event to review");
});
