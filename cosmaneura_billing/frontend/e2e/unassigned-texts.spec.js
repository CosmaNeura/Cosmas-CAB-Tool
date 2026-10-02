import { expect, test, workspaceRows } from "./sms-workspace.fixture.js";

const base = "/api/chase/communications";
const at = "2026-09-30T16:00:00.123456+00:00";
const snapshot = "11111111-1111-4111-8111-111111111111";
const item = (ref, text) => ({ ref: ref.repeat(64), sourceHash: "f".repeat(64), kind: "sms_reply", channel: "sms", text,
  at, status: "received", disposition: text === "STOP" ? "sms_opt_out" : "reply_received", mediaCount: 0,
  senderHint: "Ending 0177", reviewState: "pending", reviewedAt: null, attributedCaseId: null });

test("staff explicitly attribute or leave unknown texts; uncertain saves retry exactly without outreach", async ({ page }, testInfo) => {
  // The shared inbox remains available even when no case appears in the queue.
  await page.route("**/api/chase/worklist", (route) => route.fulfill({ json: { rows: [], communications: { available: true, mode: "durable" } } }));
  const items = [item("a", "Synthetic reply: please have staff call me."), item("b", "STOP")];
  let inbox = [...items], reviewed = [], failOnce = true; const commands = [];
  await page.route("**/api/chase/communications/unassigned-texts/count", (route) => route.fulfill({ json: {
    schemaVersion: "communications-unassigned-count.v1", pendingCount: inbox.length } }));
  await page.route("**/api/chase/communications/unassigned-texts", (route) => {
    const { state } = route.request().postDataJSON();
    return route.fulfill({ json: { schemaVersion: "communications-unassigned.v1", state, pendingCount: inbox.length,
      items: state === "pending" ? inbox : reviewed, hasMore: false, nextCursor: null } });
  });
  await page.route("**/api/chase/communications/unassigned-text-cases", (route) => {
    const { eventRef, sourceHash } = route.request().postDataJSON();
    return route.fulfill({ json: { schemaVersion: "communications-unassigned-cases.v1", eventRef, sourceHash, hasMore: false, nextCursor: null,
      items: [{ chaseId: "synthetic-target", patientLabel: "Synthetic patient with verified contact", state: "needs_review", bucket: "missing", contactSnapshotId: snapshot, verifiedAt: at }] } });
  });
  await page.route("**/api/chase/communications/review-unassigned-text", (route) => {
    const cmd = route.request().postDataJSON(); commands.push(cmd);
    expect(Object.keys(cmd).sort()).toEqual(["actionId", "chaseId", "confirmed", "contactSnapshotId", "decision", "eventRef", "sourceHash"]);
    expect(cmd.confirmed).toBe(true);
    const source = inbox.find((v) => v.ref === cmd.eventRef);
    if (source) {
      inbox = inbox.filter((v) => v.ref !== cmd.eventRef);
      reviewed.push({ ...source, reviewedAt: at, reviewState: cmd.decision === "attribute" ? "attributed" : "left_unassigned", attributedCaseId: cmd.chaseId });
    }
    if (failOnce) { failOnce = false; return route.fulfill({ status: 503, json: {} }); }
    return route.fulfill({ json: { schemaVersion: "communications-unassigned-decision.v1", pendingCount: inbox.length, receipt: { ...cmd, reviewedAt: at } } });
  });
  const mutations = [];
  page.on("request", (request) => { if (request.method() === "POST" && request.url().includes(base) && /\/(action|authorize|consent|bind|cancel)$/.test(request.url())) mutations.push(request.url()); });
  await page.goto("/");
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: "Card chase", exact: true }).click();
  const panel = page.getByRole("region", { name: "Unassigned texts", exact: true });
  await panel.getByRole("button", { name: "Unassigned texts · 2", exact: true }).click();
  await panel.getByRole("listitem").filter({ hasText: "Synthetic reply:" }).getByRole("button", { name: "Review text", exact: true }).click();
  const form = panel.getByRole("form", { name: "Review selected text" });
  await form.getByLabel("Text review decision", { exact: true }).selectOption("attribute");
  await expect(form.getByLabel("Existing case", { exact: true })).toHaveValue("");
  await expect(form.getByRole("button", { name: "Save text review" })).toBeDisabled();
  await form.getByLabel("Existing case", { exact: true }).selectOption("synthetic-target");
  await form.getByRole("checkbox").check();
  await form.getByRole("button", { name: "Save text review" }).click();
  await expect(panel.getByRole("alert")).toContainText("could not be confirmed");
  await expect(panel.getByRole("button", { name: "Refresh inbox" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeDisabled();
  await expect(form.getByLabel("Existing case", { exact: true })).toBeDisabled();
  await panel.getByRole("button", { name: "Retry same text review" }).click();
  await expect(panel.getByRole("status").filter({hasText: "Attributed to the selected case"})).toContainText("Attributed to the selected case");
  expect(commands).toHaveLength(2); expect(commands[0]).toEqual(commands[1]);
  await expect(panel.getByRole("button", { name: "Unassigned texts · 1", exact: true })).toBeVisible();
  await panel.getByRole("listitem").filter({ hasText: "STOP" }).getByRole("button", { name: "Review text", exact: true }).click();
  await form.getByLabel("Text review decision", { exact: true }).selectOption("leave_unassigned");
  await form.getByRole("checkbox").check(); await form.getByRole("button", { name: "Save text review" }).click();
  await expect(panel).toContainText("No unassigned texts await review.");
  expect(commands[2].chaseId).toBeNull(); expect(commands[2].contactSnapshotId).toBeNull();
  await panel.getByRole("button", { name: "Reviewed texts", exact: true }).click();
  await expect(panel).toContainText("Attributed by staff to synthetic-target");
  await expect(panel).toContainText("Marked reviewed · Left unassigned");
  await expect(panel).toContainText("STOP received · SMS suppression remains in effect");
  expect(mutations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("unassigned-texts-reviewed.png"), fullPage: true });
});
