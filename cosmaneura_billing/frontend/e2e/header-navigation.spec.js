import {createHash} from "node:crypto";
import {expect, test} from "./sms-workspace.fixture.js";
import {factKeys} from "../src/billingHeader.js";

// Synthetic response fixtures exercise navigation in the actual built workspace.
// Backend signoff and persistence are covered separately; no vendor is contacted.
const ids = ["SYN-NAV-A", "SYN-NAV-B"];
const source = "athena_staff_review_preview_synthetic";
const actor = {id: "11111111-1111-4111-8111-111111111111", name: "Synthetic reviewer", role: "provider"};
const hash = (value) => createHash("sha256").update(value).digest("hex");

async function setup(page, {workspace = "provider", lostSave = false} = {}) {
  const details = {}, views = {}, commands = [], persisted = new Set();
  for (const [index, id] of ids.entries()) {
    const note = `Synthetic documented visit ${id}.`;
    const pin = {encounterId: id, encounterVersion: 1, ingestionSource: source,
      noteId: `${id}:note`, noteVersion: "1", noteSha256: hash(note),
      bundleSha256: hash(id), supportingSourceDigests: []};
    details[id] = {id, encounterVersion: 1, dataClass: source, readOnly: true,
      encounterType: "office_established", placeOfService: "11", submittedEm: "99213",
      signedNote: note, transcript: "", intake: "", headerSource: pin};
    views[id] = {revision: null, revisionNumber: 0, header: {
      schemaVersion: "provider-billing-header.v1",
      headerId: `22222222-2222-4222-8222-22222222222${index + 2}`,
      expectedSource: pin, reviewStatus: "unreviewed", createdBy: actor,
      generatedAt: "2026-10-07T12:00:00Z",
      facts: factKeys.map((key) => ({key, label: key, status: "not_documented", value: null, evidenceIds: []})),
      evidence: [{id: "e1", source: "note", sourceId: pin.noteId, sourceVersion: "1",
        start: 0, end: 9, text: "Synthetic", coordinateSystem: "unicode_code_point"}],
      suggestions: [{id: "s1", kind: "EM", code: "99213", label: "Synthetic UI fixture",
        evidenceIds: ["e1"], reviewStatus: "unreviewed", basis: "Authored browser fixture"}],
      flags: [], policy: {status: "not_verified", message: "Human review required"},
      generation: {inferenceMode: "mock", runId: actor.id},
    }};
  }
  await page.route("**/api/session", (route) => route.fulfill({json: {
    views: ["provider", "coder", "chase"], defaultView: workspace,
    capabilities: {billingHeader: true, providerHandoff: true}, persona: "Synthetic reviewer",
  }}));
  await page.route("**/api/encounters", (route) => route.fulfill({json: Object.values(details)}));
  await page.route("**/api/encounters/detail", (route) => route.fulfill({json: details[route.request().postDataJSON().encounterId]}));
  await page.route("**/api/completeness", (route) => route.fulfill({json: {complete: true, results: []}}));
  await page.route("**/api/encounters/coder-view", (route) => {
    const {encounterId} = route.request().postDataJSON();
    return route.fulfill({json: {encounterId, encounterVersion: 1, readOnly: true, dataClass: source,
      reviewContext: "synthetic-browser-context", submittedEm: "99213",
      em: {kind: "EM", code: "99213", why: "Authored fixture", evidence: "Synthetic",
        evidenceSpans: [{source: "signed_note", text: "Synthetic"}]},
      dx: [], withheld: [], completeness: {complete: true, results: []}}});
  });
  await page.route(/\/api\/encounters\/header(?:\/save)?$/, (route) => {
    const body = route.request().postDataJSON(), id = body.encounterId;
    expect(body.expectedSource).toEqual(details[id].headerSource);
    if (!new URL(route.request().url()).pathname.endsWith("/save")) return route.fulfill({json: {view: views[id]}});
    commands.push(body);
    if (persisted.has(body.operationId)) return route.fulfill({json: {view: views[id], created: false}});
    const view = views[id];
    expect(body.headerId).toBe(view.header.headerId);
    expect(body.expectedRevision).toBe(view.revisionNumber);
    const revision = {id: actor.id, number: view.revisionNumber + 1, headerId: view.header.headerId,
      expectedSource: body.expectedSource, reviewStatus: "unreviewed", actor,
      createdAt: "2026-10-07T12:01:00Z", reason: body.reason, draft: body.draft,
      changes: [{path: "note", label: "Reviewer note", before: view.revision?.draft.note || "", after: body.draft.note}]};
    revision.history = [...(view.revision?.history || []), {...revision}];
    views[id] = {...view, revision, revisionNumber: revision.number};
    persisted.add(body.operationId);
    return route.fulfill(lostSave ? {status: 503, json: {}} : {json: {view: views[id], created: true}});
  });
  await page.goto("/");
  await page.getByRole("button", {name: ids[0], exact: true}).click();
  const header = page.getByRole("region", {name: "Billing header", exact: true});
  await header.getByRole("button", {name: "Edit header", exact: true}).click();
  await header.getByLabel("Reviewer note (optional)").fill("Keep my unsaved review.");
  await header.getByLabel("Reason for edit (required)").fill("Compared the synthetic documentation.");
  return {header, commands, navigation: page.getByRole("navigation", {name: "Workspace"}),
    prompt: page.getByRole("group", {name: "Confirm discarding header edits"})};
}

for (const workspace of ["provider", "coder"]) {
  test(`${workspace} header draft survives canceled case and workspace navigation`, async ({page}) => {
    const {header, prompt, navigation} = await setup(page, {workspace});
    expect(await page.evaluate(() => {
      const event = new Event("beforeunload", {cancelable: true});
      window.dispatchEvent(event); return event.defaultPrevented;
    })).toBe(true);
    await page.getByRole("button", {name: ids[1], exact: true}).click();
    await expect(prompt).toContainText(ids[1]);
    await prompt.getByRole("button", {name: "Keep editing", exact: true}).click();
    await expect(header.getByLabel("Reviewer note (optional)")).toHaveValue("Keep my unsaved review.");
    const other = workspace === "provider" ? "Coder review" : "Provider";
    await navigation.getByRole("button", {name: other, exact: true}).click();
    await expect(prompt).toBeVisible();
    await prompt.getByRole("button", {name: "Keep editing", exact: true}).click();
    await expect(header.getByLabel("Reviewer note (optional)")).toHaveValue("Keep my unsaved review.");
    await page.getByRole("button", {name: ids[1], exact: true}).click();
    await prompt.getByRole("button", {name: "Discard edits and continue", exact: true}).click();
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toHaveAttribute("aria-current", "true");
    await expect(header.getByRole("button", {name: "Edit header", exact: true})).toBeVisible();
    await expect(header.getByLabel("Reviewer note (optional)")).toHaveCount(0);
  });

  test(`${workspace} uncertain header save blocks stale discard and retries the identical operation`, async ({page}) => {
    const {header, prompt, navigation, commands} = await setup(page, {workspace, lostSave: true});
    await page.getByRole("button", {name: ids[1], exact: true}).click();
    await expect(prompt).toBeVisible();
    await header.getByRole("button", {name: "Save header edits", exact: true}).click();
    await expect(header.getByRole("button", {name: "Retry same request", exact: true})).toBeVisible();
    await expect(prompt).toHaveCount(0);
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toBeDisabled();
    await expect(navigation.getByRole("button", {name: "Card chase", exact: true})).toBeDisabled();
    await expect(navigation.getByRole("button", {name: workspace === "provider" ? "Coder review" : "Provider", exact: true})).toBeDisabled();
    if (workspace === "provider") await expect(page.getByRole("link", {name: "Try a synthetic note handoff"})).not.toHaveAttribute("href");
    await header.getByRole("button", {name: "Retry same request", exact: true}).click();
    await expect(header).toContainText("Header edits saved");
    expect(commands).toHaveLength(2);
    expect(commands[0]).toEqual(commands[1]);
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toBeEnabled();
    await page.getByRole("button", {name: ids[1], exact: true}).click();
    await expect(prompt).toHaveCount(0);
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toHaveAttribute("aria-current", "true");
  });
}

test("successful header save clears a pending discard request", async ({page}) => {
  const {header, prompt} = await setup(page);
  await page.getByRole("button", {name: ids[1], exact: true}).click();
  await expect(prompt).toBeVisible();
  await header.getByRole("button", {name: "Save header edits", exact: true}).click();
  await expect(header).toContainText("Header edits saved");
  await expect(prompt).toHaveCount(0);
  await expect(page.getByRole("button", {name: ids[0], exact: true})).toHaveAttribute("aria-current", "true");
  expect(await page.evaluate(() => {
    const event = new Event("beforeunload", {cancelable: true});
    window.dispatchEvent(event); return event.defaultPrevented;
  })).toBe(false);
});
