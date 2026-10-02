import {readFileSync} from "node:fs";
import {createHash} from "node:crypto";
import {expect, test, workspaceRows} from "./sms-workspace.fixture.js";
import {factKeys} from "../src/billingHeader.js";

const id = "SYN-ATHENA-LAUNCH", path = "/?view=header&encounterId=" + id;
const actor = {id: "11111111-1111-4111-8111-111111111111", name: "Synthetic reviewer", role: "provider"};
const uuid = "22222222-2222-4222-8222-222222222222";
const hash = (text) => createHash("sha256").update(text).digest("hex");

async function setup(page, {operational = false, allowed = true, saved = false, lostGenerate = false, lostSave = false, malformed = false} = {}) {
  const note = "Synthetic follow-up note.";
  const source = operational ? "athena_staff_review_operational" : "athena_staff_review_preview_synthetic";
  const pin = {encounterId: id, encounterVersion: 1, ingestionSource: source, noteId: "athena:note",
    noteVersion: "1", noteSha256: hash(note), bundleSha256: "b".repeat(64), supportingSourceDigests: []};
  const detail = {id, encounterVersion: 1, readOnly: !operational, dataClass: operational ? "operational" : source,
    encounterType: "office_established", placeOfService: "11", signedNote: note, transcript: "", intake: "",
    headerSource: pin, sourceVisitContext: {schemaVersion: "cab.source-visit-context.v1", expectedSource: pin,
      origin: "athena_import", sourceSha256: "c".repeat(64), serviceDate: "2026-09-29",
      noteStatus: "CLOSED", statusMeaning: "source_reported"},
    ...(operational ? {inferencePolicy: {allowed, reason: allowed ? null : "operational_source_model_not_approved"}} : {})};
  const header = {schemaVersion: "provider-billing-header.v1", headerId: uuid, expectedSource: pin,
    reviewStatus: "unreviewed", createdBy: actor, generatedAt: "2026-09-30T12:00:00Z",
    facts: factKeys.map((key) => ({key, label: key, status: "not_documented", value: null, evidenceIds: []})),
    evidence: [{id: "e1", source: "note", sourceId: pin.noteId, sourceVersion: "1",
      start: 0, end: 9, text: "Synthetic", coordinateSystem: "unicode_code_point"}],
    suggestions: [{id: "s1", kind: "EM", code: "99213", label: "Authored UI fixture",
      evidenceIds: ["e1"], reviewStatus: "unreviewed", basis: "Synthetic browser contract fixture"}],
    flags: [], policy: {status: "not_verified", message: "Human review required"},
    generation: {inferenceMode: operational ? "azure_openai" : "mock", runId: actor.id,
      ...(operational ? {modelInputProof: {schemaVersion: "cab-model-input-proof.v1", policyId: actor.id,
        policySha256: "a".repeat(64), sourceSha256: "b".repeat(64), serviceSha256: "c".repeat(64),
        expiresAt: "2020-01-01T00:00:00Z"}} : {})}};
  let view = saved ? {header, revision: null, revisionNumber: 0} : null;
  let detailReads = 0, savedOperation = null; const commands = [];
  await page.route("**/api/session", (route) => route.fulfill({json: {
    views: ["provider"], defaultView: "provider", persona: "Synthetic provider", capabilities: {billingHeader: true}}}));
  await page.route("**/api/encounters/detail", (route) => {
    detailReads++; expect(route.request().postDataJSON()).toEqual({encounterId: id});
    return route.fulfill({json: malformed ? {...detail, signedNote: note + " altered"} : detail});
  });
  await page.route(/\/api\/encounters\/header(?:\/generate|\/save)?$/, (route) => {
    const routePath = new URL(route.request().url()).pathname, body = route.request().postDataJSON();
    expect(body.encounterId).toBe(id); expect(body.expectedSource).toEqual(pin);
    if (routePath.endsWith("/generate")) {
      commands.push({path: routePath, body});
      if (!view) {
        view = {header, revision: null, revisionNumber: 0};
        if (lostGenerate) return route.fulfill({status: 503, json: {}});
      }
      return route.fulfill({json: {view, created: commands.length === 1}});
    }
    if (routePath.endsWith("/save")) {
      commands.push({path: routePath, body});
      if (savedOperation === body.operationId) return route.fulfill({json: {view, created: false}});
      expect(body.headerId).toBe(uuid); expect(body.expectedRevision).toBe(view.revisionNumber);
      const revision = {id: actor.id, number: view.revisionNumber + 1, headerId: uuid, expectedSource: pin,
        reviewStatus: "unreviewed", actor, createdAt: "2026-10-02T12:00:00Z", reason: body.reason,
        draft: body.draft, changes: [{path: "note", label: "Reviewer note", before: "", after: body.draft.note}]};
      revision.history = [...(view.revision?.history || []), {...revision}];
      view = {...view, revision, revisionNumber: revision.number};
      savedOperation = body.operationId;
      if (lostSave) return route.fulfill({status: 503, json: {}});
      return route.fulfill({json: {view, created: true}});
    }
    return route.fulfill({json: {view}});
  });
  await page.goto(path);
  return {region: page.getByRole("region", {name: "Billing header", exact: true}),
    commands, detailReads: () => detailReads, detail, view: () => view};
}

test("reviewed Athena note opens one focused header, edits/copies and reloads without changing source", async ({page, context}, info) => {
  const state = await setup(page); const header = state.region;
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await expect(page.getByRole("navigation", {name: "Workspace"})).toHaveCount(0);
  await expect(header.getByText("2026-09-29", {exact: true})).toBeVisible();
  await header.getByRole("button", {name: "Generate billing header", exact: true}).click();
  await expect(header.getByText("99213", {exact: true})).toBeVisible();
  await header.getByRole("button", {name: "Edit header", exact: true}).click();
  await header.getByLabel("Reviewer note (optional)").fill("Checked this synthetic source.");
  await header.getByLabel("Reason for edit (required)").fill("Reviewed source documentation.");
  await page.getByRole("button", {name: "Refresh encounter", exact: true}).click();
  await expect(page.getByRole("group", {name: "Confirm encounter refresh"})).toBeVisible();
  await page.getByRole("button", {name: "Keep editing", exact: true}).click();
  await expect(header.getByLabel("Reviewer note (optional)")).toHaveValue("Checked this synthetic source.");
  await header.getByRole("button", {name: "Save header edits", exact: true}).click();
  await expect(header.getByRole("status")).toContainText("Header edits saved");
  await header.getByRole("button", {name: "Copy header with evidence", exact: true}).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("Checked this synthetic source.");
  expect(copied).toContain("Date of service: 2026-09-29");
  expect(copied).toContain("upstream signature not independently verified");
  await page.getByRole("button", {name: "Refresh encounter", exact: true}).click();
  await expect(header).toContainText("Checked this synthetic source.");
  expect(state.detailReads()).toBe(2); expect(state.detail.signedNote).toBe("Synthetic follow-up note.");
  await page.screenshot({path: info.outputPath("focused-athena-header-desktop.png"), fullPage: true});
  await page.setViewportSize({width: 900, height: 1100});
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: info.outputPath("focused-athena-header-tablet.png"), fullPage: true});
});

for (const saved of [false, true]) test("current model hold " + (saved ? "allows editing a saved header after proof expiry" : "blocks new generation"), async ({page}) => {
  const state = await setup(page, {operational: true, allowed: false, saved});
  await expect(state.region).toContainText("New coding generation awaits approval");
  if (saved) {
    await state.region.getByRole("button", {name: "Edit header", exact: true}).click();
    await state.region.getByLabel("Reviewer note (optional)").fill("Historical header still reviewable.");
    await state.region.getByLabel("Reason for edit (required)").fill("Compared unchanged source.");
    await state.region.getByRole("button", {name: "Save header edits", exact: true}).click();
    await expect(state.region).toContainText("Header edits saved");
    expect(state.commands.map((c) => c.path)).toEqual(["/api/encounters/header/save"]);
  } else {
    await expect(state.region.getByRole("button", {name: "Generate billing header", exact: true})).toBeDisabled();
    expect(state.commands).toEqual([]);
  }
});

test("lost generation keeps refresh locked and retries the exact operation", async ({page}) => {
  const state = await setup(page, {lostGenerate: true});
  await state.region.getByRole("button", {name: "Generate billing header", exact: true}).click();
  await expect(state.region.getByRole("button", {name: "Retry same request", exact: true})).toBeVisible();
  await expect(page.getByRole("button", {name: "Refresh encounter", exact: true})).toBeDisabled();
  await state.region.getByRole("button", {name: "Retry same request", exact: true}).click();
  await expect(state.region).toContainText("Original pipeline suggestions");
  await expect(page.getByRole("button", {name: "Refresh encounter", exact: true})).toBeEnabled();
  expect(state.commands).toHaveLength(2); expect(state.commands[0]).toEqual(state.commands[1]);
});

test("source mismatch never mounts a header or generates", async ({page}) => {
  const state = await setup(page, {malformed: true});
  await expect(page.getByRole("alert")).toContainText("This encounter could not be verified");
  await expect(state.region).toHaveCount(0); expect(state.commands).toEqual([]);
});

test("invalid link makes no encounter request; sign-in returns to the exact launch", async ({page}) => {
  const requests = [];
  page.on("request", (r) => {if (r.url().includes("/api/")) requests.push(r.url());});
  await page.goto(path + "&note=untrusted");
  await expect(page.getByRole("alert")).toContainText("incomplete or invalid");
  expect(requests).toEqual([]);
  await page.route("**/api/session", (route) => route.fulfill({status: 401, json: {}}));
  await page.goto(path);
  await expect(page.getByRole("link", {name: "Sign in with Microsoft"})).toHaveAttribute("href",
    "/.auth/login/aad?post_login_redirect_uri=%2F%3Fview%3Dheader%26encounterId%3DSYN-ATHENA-LAUNCH");
});

for (const available of [false, true]) test("reviewed import shortcut follows header capability " + available + " and protects pending source reads", async ({page}) => {
  const golden = JSON.parse(readFileSync(new URL("../src/athenaReview.fixture.json", import.meta.url)));
  const chaseId = workspaceRows[0].chaseId;
  const view = {...structuredClone(golden.source), chaseId, revision: 3,
    applied: structuredClone(golden.applied), billingImport: structuredClone(golden.import)};
  await page.route("**/api/session", (route) => route.fulfill({json: {views: ["chase", "provider"], defaultView: "chase",
    capabilities: {manageChase: true, billingHeader: available}}}));
  await page.route("**/api/encounters", (route) => route.fulfill({json: []}));
  await page.route("**/api/chase/task", (route) => route.fulfill({json: {...workspaceRows[0],
    open: true, assigned: true, allowedActions: [], contact: null,
    instruction: "Review the synthetic imported note.",
    systemOfRecord: {name: "Synthetic source", instruction: "No practice-record link."},
    athenaReview: {available: true, mode: "mock", billingAvailable: true}}}));
  await page.route("**/api/athena/card-chase", (route) => route.fulfill({json: {view}}));
  await page.route("**/api/athena/card-chase/read", (route) => route.fulfill({status: 503, json: {}}));
  await page.goto("/");
  await page.getByRole("button", {name: /Synthetic patient A/}).click();
  await page.getByRole("tab", {name: "Athena review", exact: true}).click();
  const panel = page.getByRole("region", {name: "Athena case review", exact: true});
  await expect(panel).toContainText(view.billingImport.encounterId);
  const shortcut = panel.getByRole("link", {name: "Open billing header (new tab)", exact: true});
  if (!available) { await expect(shortcut).toHaveCount(0); return; }
  await expect(shortcut).toHaveAttribute("href", "/?view=header&encounterId=" + view.billingImport.encounterId);
  await expect(shortcut).toHaveAttribute("target", "_blank");
  await expect(shortcut).toHaveAttribute("rel", "noopener noreferrer");
  await panel.getByRole("button", {name: "Read source", exact: true}).click();
  await expect(panel.getByRole("button", {name: "Retry same request", exact: true})).toBeVisible();
  await expect(shortcut).toHaveAttribute("aria-disabled", "true");
  await expect(shortcut).not.toHaveAttribute("href");
  await expect(page.getByRole("tab", {name: "Athena review", exact: true})).toHaveAttribute("aria-selected", "true");
});

test("an open discard confirmation cannot erase an uncertain header save", async ({page}) => {
  const state = await setup(page, {saved: true, lostSave: true});
  await state.region.getByRole("button", {name: "Edit header", exact: true}).click();
  await state.region.getByLabel("Reviewer note (optional)").fill("Keep this exact saved operation.");
  await state.region.getByLabel("Reason for edit (required)").fill("Source reviewed.");
  await page.getByRole("button", {name: "Refresh encounter", exact: true}).click();
  await expect(page.getByRole("button", {name: "Discard edits and refresh", exact: true})).toBeEnabled();
  await state.region.getByRole("button", {name: "Save header edits", exact: true}).click();
  await expect(state.region.getByRole("button", {name: "Retry same request", exact: true})).toBeVisible();
  await expect(page.getByRole("button", {name: "Discard edits and refresh", exact: true})).toBeDisabled();
  await state.region.getByRole("button", {name: "Retry same request", exact: true}).click();
  await expect(state.region).toContainText("Header edits saved");
  await expect(page.getByRole("group", {name: "Confirm encounter refresh"})).toHaveCount(0);
  expect(state.detailReads()).toBe(1);
  expect(state.commands).toHaveLength(2); expect(state.commands[0]).toEqual(state.commands[1]);
});
