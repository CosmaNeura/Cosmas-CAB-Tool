import {readFileSync} from "node:fs";
import {expect, test, workspaceRows} from "./sms-workspace.fixture.js";

const golden = JSON.parse(readFileSync(new URL("../src/athenaReview.fixture.json", import.meta.url)));
const base = "/api/athena/card-chase", chaseId = workspaceRows[0].chaseId;
const binding = {schemaVersion: "athena-source-binding.v1", serverPracticeId: "local-test", chaseId,
  environment: "preview", vendorPracticeId: "195900", sourcePatientId: "60178", connectionRevision: 1,
  bindingRevision: 1, profileSha256: "a".repeat(64), scopeSha256: "b".repeat(64)};

async function setup(page, {lostApply = false, missingBinding = false, invalidConnection = false} = {}) {
  let view = {schemaVersion: "athena-case-review.v1", chaseId, revision: 0, reviewId: null, candidate: null, applied: null};
  let applyCount = 0, taskReads = 0;
  const commands = [];
  const task = {...workspaceRows[0], open: true, assigned: true, allowedActions: [], contact: null,
    instruction: "Review mapped source information.", systemOfRecord: {name: "Synthetic source", instruction: "No record link."},
    athenaReview: {available: true, mode: "configured", billingAvailable: false}};
  await page.route("**/api/chase/task", (route) => { taskReads++; return route.fulfill({json: task}); });
  await page.route("**/api/athena/**", (route) => {
    const path = new URL(route.request().url()).pathname, body = route.request().postDataJSON();
    const connection = () => ({workflow: "configured", state: missingBinding ? "binding_missing" : "ready",
      readAllowed: !missingBinding, currentReviewMatchesBinding: view.revision > 0, binding: missingBinding ? null : binding});
    if (path === base) return route.fulfill({json: invalidConnection === "omitted" ? {view}
      : {view, connection: invalidConnection === "null" ? null : connection()}});
    commands.push({path, body});
    expect(missingBinding).toBe(false); expect(body.chaseId).toBe(chaseId);
    if (path === `${base}/read`) {
      expect(body.expectedBindingSha256).toBe(binding.scopeSha256);
      expect(body).not.toHaveProperty("sourcePatientId"); expect(body.expectedRevision).toBe(0);
      view = {...structuredClone(golden.source), schemaVersion: "athena-case-review.v2", chaseId, mode: "preview",
        sourceBinding: binding, candidate: {...structuredClone(golden.source.candidate), sourceBinding: binding}};
    } else if (path === `${base}/apply`) {
      expect(body.expectedRevision).toBe(1); expect(body.confirmedPatientMatch).toBe(true);
      expect(body.selectedPhoneField).toBe("mobile_phone"); expect(body.selectedEmail).toBe(true);
      expect(body.selectedPolicyIds).toEqual(golden.source.candidate.insurance.policies.map((p) => p.candidateId));
      expect(body.confirmedPolicyReview).toBe(true); expect(body.reviewReason).toBe("Compared the synthetic patient and exact fields.");
      if (!view.applied) {
        applyCount++;
        view = {...view, revision: 2, applied: {...structuredClone(golden.applied), sourceBinding: binding, operationId: body.operationId}};
        if (lostApply) return route.fulfill({status: 503, json: {detail: "Synthetic lost response after save"}});
      }
    } else throw new Error(`Unexpected Athena endpoint ${path}`);
    return route.fulfill({json: {view, connection: connection(), operationReceipt: {
      operationId: body.operationId, revision: view.revision, action: path.endsWith("/read") ? "athena.refresh" : "athena.apply",
      replayed: path.endsWith("/apply") && commands.filter((c) => c.path.endsWith("/apply")).length > 1,
      current: true, sourceBinding: binding,
    }}});
  });
  await page.goto("/"); await page.getByRole("button", {name: /Synthetic patient A/}).click();
  await page.getByRole("tab", {name: "Athena review", exact: true}).click();
  const panel = page.getByRole("region", {name: "Athena case review", exact: true});
  if (!invalidConnection) await expect(panel.getByRole("heading", {name: "Mapped Athena source"})).toBeVisible();
  return {panel, commands, applyCount: () => applyCount, taskReads: () => taskReads};
}

async function selectFields(panel) {
  await expect(panel.getByLabel("Synthetic Athena patient", {exact: true})).toHaveCount(0);
  await panel.getByRole("button", {name: "Read mapped source", exact: true}).click();
  await expect(panel.getByRole("heading", {name: "Source fields requiring review"})).toBeVisible();
  await expect(panel.getByLabel("Phone to apply", {exact: true})).toHaveValue("");
  for (const checkbox of await panel.getByRole("checkbox").all()) await expect(checkbox).not.toBeChecked();
  await expect(panel.getByRole("button", {name: "Apply selected fields", exact: true})).toBeDisabled();
  await panel.getByRole("checkbox", {name: /I compared the source identity/}).check();
  await panel.getByLabel("Phone to apply", {exact: true}).selectOption("mobile_phone");
  await panel.getByRole("checkbox", {name: /Apply email:/}).check();
  for (const checkbox of await panel.getByRole("checkbox", {name: /Select insurance observation/}).all()) await checkbox.check();
  await panel.getByRole("checkbox", {name: /I reviewed the selected insurance/}).check();
  await panel.getByLabel("Application review reason", {exact: true}).fill("Compared the synthetic patient and exact fields.");
}

for (const lostApply of [false, true]) test(`configured Athena fields apply with explicit review${lostApply ? " and exact retry" : ""}`, async ({page}, testInfo) => {
  const state = await setup(page, {lostApply}); const {panel} = state;
  await selectFields(panel); await panel.getByRole("button", {name: "Apply selected fields", exact: true}).click();
  if (lostApply) {
    await expect(panel.getByRole("button", {name: "Retry same request", exact: true})).toBeEnabled();
    await expect(page.getByRole("button", {name: "Refresh", exact: true})).toBeDisabled();
    await expect(page.getByRole("button", {name: /Synthetic patient A/})).toBeDisabled();
    await panel.getByRole("button", {name: "Retry same request", exact: true}).click();
  }
  await expect(panel).toContainText("Applied case information"); await expect(panel).toContainText("Consent unknown");
  await expect(page.getByRole("button", {name: "Refresh", exact: true})).toBeEnabled();
  await expect(page.getByRole("tab", {name: "Athena review", exact: true})).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("button", {name: /Synthetic patient A/})).toContainText("In progress");
  await expect(panel.getByRole("button", {name: "Find encounters", exact: true})).toHaveCount(0);
  await expect(panel.getByRole("button", {name: "Open CAB encounter", exact: true})).toHaveCount(0);
  expect(state.applyCount()).toBe(1); await expect.poll(state.taskReads).toBe(2);
  const writes = state.commands.filter((c) => c.path.endsWith("/apply")); expect(writes).toHaveLength(lostApply ? 2 : 1);
  if (lostApply) expect(writes[0]).toEqual(writes[1]);
  if (!lostApply) {
    await page.screenshot({path: testInfo.outputPath("athena-reviewed-fields-desktop.png"), fullPage: true});
    await page.setViewportSize({width: 900, height: 1100});
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path: testInfo.outputPath("athena-reviewed-fields-tablet.png"), fullPage: true});
  }
});

test("missing Athena mapping stays explicit and does not offer a patient override", async ({page}) => {
  const {panel, commands} = await setup(page, {missingBinding: true});
  await expect(panel).toContainText("No Athena patient mapping is configured for this case.");
  await expect(panel.getByRole("button", {name: "Read mapped source", exact: true})).toBeDisabled();
  await expect(panel.getByLabel("Synthetic Athena patient", {exact: true})).toHaveCount(0);
  expect(commands).toEqual([]);
});

for (const invalidConnection of ["omitted", "null"]) test(`configured Athena rejects ${invalidConnection} initial connection without legacy fallback`, async ({page}) => {
  const {panel, commands} = await setup(page, {invalidConnection});
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(panel).toContainText("Source review unavailable.");
  await expect(panel.getByLabel("Synthetic Athena patient", {exact: true})).toHaveCount(0);
  await expect(panel.getByRole("button", {name: "Read source", exact: true})).toHaveCount(0);
  await expect(panel.getByRole("button", {name: "Read mapped source", exact: true})).toHaveCount(0);
  expect(commands).toEqual([]);
});
