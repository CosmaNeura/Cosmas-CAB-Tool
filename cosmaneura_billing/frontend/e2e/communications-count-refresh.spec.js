import { expect, test, workspaceRows } from "./sms-workspace.fixture.js";

const base = "/api/chase/communications";
const at = "2026-09-30T16:00:00.000000+00:00";
const reply = { eventId: "synthetic-case-b-reply", kind: "sms_reply", channel: "sms", direction: "inbound",
  text: "Synthetic reply for case B only.", status: "received", disposition: "reply_received",
  occurredAt: at, needsStaffReview: true, mediaCount: 0, operationId: null };

function conversation(chaseId, events) {
  return { schemaVersion: "chase-communications.v1", chaseId, mode: "durable", truncated: false,
    enabledChannels: ["sms"], operations: [], events, reviewPendingCount: events.length, heldInboundCount: 0,
    suppression: { sms: false, automatedVoice: false }, contact: null, bindings: {},
    actions: { sms: { allowed: true, reason: "", authorization: { allowed: false, reason: "communications_dispatch_disabled" } },
      automatedVoice: { allowed: false, reason: "communications_channel_disabled" } },
    policy: { hash: "a".repeat(64), approved: false, canApprove: true, outboundEnabled: false,
      practiceName: "Synthetic Practice", timezone: "UTC", quietHours: [21, 8], maxTotalTouches: 8, maxTouches24h: 2 },
    templates: [], callScript: "Unused synthetic call script." };
}

test("another case's incoming texts update badges without replacing the open SMS draft", async ({ page, baseURL }, testInfo) => {
  expect(baseURL).toMatch(/^http:\/\/127\.0\.0\.1:[0-9]+$/);
  await page.clock.install();
  let caseB = null, incoming = [], unassigned = 0, boardReads = 0, inboxReads = 0, failCounts = false;
  const taskReads = [], mutations = [], conversationReads = [], pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/chase/task") taskReads.push(request.postDataJSON());
  });
  await page.route("**/api/chase/worklist", async (route) => {
    boardReads++;
    if (failCounts) return route.fulfill({status: 503, json: {detail: "Synthetic count read failure"}});
    const value = { rows: structuredClone(workspaceRows), communications: { available: true, mode: "durable" } };
    caseB = value.rows.find((row) => row.patientLabel === "Synthetic patient B")?.chaseId;
    expect(caseB).toBeTruthy();
    value.rows = value.rows.map((row) => ({ ...row, communicationReview: {
      pending: row.chaseId === caseB ? incoming.length : 0, held: 0,
    } }));
    await route.fulfill({ json: value });
  });
  await page.route("**/api/chase/communications**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `${base}/unassigned-texts/count`) {
      inboxReads++;
      if (failCounts) return route.fulfill({status: 503, json: {detail: "Synthetic count read failure"}});
      return route.fulfill({ json: { schemaVersion: "communications-unassigned-count.v1", pendingCount: unassigned } });
    }
    const body = route.request().postDataJSON();
    if (path === base) {
      conversationReads.push(body.chaseId);
      return route.fulfill({ json: conversation(body.chaseId, body.chaseId === caseB ? incoming : []) });
    }
    if (path === `${base}/review-events`) return route.fulfill({ json: {
      schemaVersion: "communications-review.v1", chaseId: body.chaseId,
      totalPending: 0, heldPending: 0, items: [], truncated: false,
    } });
    mutations.push({ path, body });
    return route.fulfill({ status: 400, json: { detail: "Unexpected mutation in read-only count test" } });
  });

  await page.goto("/");
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Card chase", exact: true }).click();
  const patientA = page.getByRole("button", { name: /Synthetic patient A/ });
  const patientB = page.getByRole("button", { name: /Synthetic patient B/ });
  const opened = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/chase/task");
  await patientA.click();
  const selectedCase = (await (await opened).json()).chaseId;
  await page.getByRole("tab", { name: "Communications", exact: true }).click();
  const panel = page.getByRole("region", { name: "Case communications", exact: true });
  const draft = panel.getByRole("textbox", { name: "SMS draft", exact: true });
  const unfinished = "Synthetic Practice: Unfinished reply for case A. Reply STOP to stop.";
  await draft.fill(unfinished);
  await expect(patientB).not.toContainText("communication event");
  const inbox = page.getByRole("region", { name: "Unassigned texts", exact: true });
  await expect(inbox.getByRole("button", { name: "Unassigned texts · 0", exact: true })).toHaveAttribute("aria-expanded", "false");
  const detailReadsBefore = taskReads.length;
  const boardReadsBefore = boardReads, inboxReadsBefore = inboxReads;

  incoming = [reply]; unassigned = 1;
  await page.clock.runFor(15_001);
  await expect(patientB).toContainText("1 communication event to review");
  await expect(inbox.getByRole("button", { name: "Unassigned texts · 1", exact: true })).toHaveAttribute("aria-expanded", "false");
  expect(boardReads).toBeGreaterThan(boardReadsBefore);
  expect(inboxReads).toBeGreaterThan(inboxReadsBefore);
  await expect(patientA).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("tab", { name: "Communications", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(draft).toHaveValue(unfinished);
  await expect(panel.getByRole("list", { name: "SMS thread" })).not.toContainText(reply.text);

  failCounts = true;
  await page.clock.runFor(15_001);
  await expect(page.getByText("Communication counts may be out of date.", {exact: false})).toBeVisible();
  await expect(inbox.getByText("The unassigned text count may be out of date.", {exact: false})).toBeVisible();
  await expect(patientB).toContainText("1 communication event to review");
  await expect(inbox.getByRole("button", {name: "Unassigned texts · 1", exact: true})).toBeVisible();
  await expect(draft).toHaveValue(unfinished);
  failCounts = false;

  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const hiddenBoardReads = boardReads, hiddenInboxReads = inboxReads;
  incoming = [reply, { ...reply, eventId: "synthetic-case-b-second-reply" }]; unassigned = 2;
  await page.clock.runFor(30_001);
  expect(boardReads).toBe(hiddenBoardReads);
  expect(inboxReads).toBe(hiddenInboxReads);
  await expect(patientB).toContainText("1 communication event to review");
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(patientB).toContainText("2 communication events to review");
  await expect(inbox.getByRole("button", { name: "Unassigned texts · 2", exact: true })).toHaveAttribute("aria-expanded", "false");
  await expect(draft).toHaveValue(unfinished);
  await expect(patientA).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("Communication counts may be out of date.", {exact: false})).toHaveCount(0);
  await expect(inbox.getByText("The unassigned text count may be out of date.", {exact: false})).toHaveCount(0);
  expect(taskReads).toHaveLength(detailReadsBefore);
  expect(conversationReads.every((id) => id === selectedCase)).toBe(true);
  expect(mutations).toEqual([]);
  expect(pageErrors).toEqual([]);
  await page.screenshot({path: testInfo.outputPath("counts-with-draft-preserved.png"), fullPage: true});
});


for (const decision of ["leave_unassigned", "attribute"]) test(`inbox ${decision} preserves the open draft and case identity`, async ({ page }) => {
  await page.clock.install();
  const selected = workspaceRows[0].chaseId, taskReads = [], commands = [];
  const item = {ref: "b".repeat(64), sourceHash: "f".repeat(64), kind: "sms_reply", channel: "sms",
    text: reply.text, at, status: "received", disposition: "reply_received", mediaCount: 0,
    senderHint: "Ending 0177", reviewState: "pending", reviewedAt: null, attributedCaseId: null};
  let saved = false;
  page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/chase/task") taskReads.push(request.postDataJSON()); });
  await page.route("**/api/chase/worklist", (route) => route.fulfill({json: {
    rows: workspaceRows.map((row) => ({...row, communicationReview: {pending: saved && decision === "attribute" && row.chaseId === selected ? 1 : 0, held: 0}})),
    communications: {available: true, mode: "durable"},
  }}));
  await page.route("**/api/chase/communications**", (route) => {
    const path = new URL(route.request().url()).pathname, body = route.request().postDataJSON();
    if (path === base) return route.fulfill({json: conversation(body.chaseId, saved && decision === "attribute" ? [reply] : [])});
    if (path === `${base}/unassigned-texts/count`) return route.fulfill({json: {schemaVersion: "communications-unassigned-count.v1", pendingCount: saved ? 0 : 1}});
    if (path === `${base}/unassigned-texts`) return route.fulfill({json: {schemaVersion: "communications-unassigned.v1", state: "pending", pendingCount: saved ? 0 : 1, items: saved ? [] : [item], hasMore: false, nextCursor: null}});
    if (path === `${base}/unassigned-text-cases`) return route.fulfill({json: {schemaVersion: "communications-unassigned-cases.v1", eventRef: item.ref, sourceHash: item.sourceHash, hasMore: false, nextCursor: null,
      items: [{...workspaceRows[0], contactSnapshotId: "11111111-1111-4111-8111-111111111111", verifiedAt: at}]}});
    if (path === `${base}/review-unassigned-text`) {
      commands.push(body); saved = true;
      return route.fulfill({json: {schemaVersion: "communications-unassigned-decision.v1", pendingCount: 0, receipt: {...body, reviewedAt: at}}});
    }
    throw new Error(`Unexpected request ${path}`);
  });
  await page.goto("/");
  const row = page.getByRole("button", {name: /Synthetic patient A/});
  await row.click(); await page.getByRole("tab", {name: "Communications", exact: true}).click();
  const panel = page.getByRole("region", {name: "Case communications", exact: true});
  const draft = panel.getByRole("textbox", {name: "SMS draft", exact: true});
  await draft.fill("Synthetic unfinished text that must survive inbox review.");
  const inbox = page.getByRole("region", {name: "Unassigned texts", exact: true});
  await inbox.getByRole("button", {name: "Unassigned texts · 1", exact: true}).click();
  await inbox.getByRole("button", {name: "Review text", exact: true}).click();
  const form = inbox.getByRole("form", {name: "Review selected text"});
  await form.getByLabel("Text review decision", {exact: true}).selectOption(decision);
  if (decision === "attribute") await form.getByLabel("Existing case", {exact: true}).selectOption(selected);
  await form.getByRole("checkbox").check(); await form.getByRole("button", {name: "Save text review"}).click();
  await expect(inbox).toContainText("No unassigned texts await review.");
  await page.clock.runFor(15_001);
  await expect(draft).toHaveValue("Synthetic unfinished text that must survive inbox review.");
  await expect(row).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("tab", {name: "Communications", exact: true})).toHaveAttribute("aria-selected", "true");
  expect(taskReads).toEqual([{chaseId: selected}]); expect(commands).toHaveLength(1);
  expect(commands[0].decision).toBe(decision); expect(commands[0].chaseId).toBe(decision === "attribute" ? selected : null);
  if (decision === "attribute") await expect(panel.getByRole("list", {name: "SMS thread"})).toContainText(reply.text);
});
