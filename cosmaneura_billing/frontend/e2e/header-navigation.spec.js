import {createHash} from "node:crypto";
import {expect, test} from "./sms-workspace.fixture.js";
import {factKeys, HEADER_REQUEST_TIMEOUT_MS} from "../src/billingHeader.js";

// Synthetic response fixtures exercise navigation in the actual built workspace.
// Backend signoff and persistence are covered separately; no vendor is contacted.
const ids = ["SYN-NAV-A", "SYN-NAV-B"];
const source = "athena_staff_review_preview_synthetic";
const actor = {id: "11111111-1111-4111-8111-111111111111", name: "Synthetic reviewer", role: "provider"};
const hash = (value) => createHash("sha256").update(value).digest("hex");

async function setup(page, {workspace = "provider", lostSave = false, holdRead = false, holdSave = false, operationalDenied = false, editHeader = true, headers = true} = {}) {
  const details = {}, views = {}, commands = [], persisted = new Set();
  const sourceKind = operationalDenied ? "provider_completed_note" : source;
  let coderRequests = 0;
  let releaseRead, releaseSave, readHeld = false;
  for (const [index, id] of ids.entries()) {
    const note = `Synthetic documented visit ${id}.`;
    const pin = {encounterId: id, encounterVersion: 1, ingestionSource: sourceKind,
      noteId: `${id}:note`, noteVersion: "1", noteSha256: hash(note),
      bundleSha256: hash(id), supportingSourceDigests: []};
    details[id] = {id, encounterVersion: 1, dataClass: operationalDenied ? "operational" : source, readOnly: !operationalDenied,
      ...(operationalDenied ? {ingestionSource: sourceKind,
        inferencePolicy: {allowed: false, reason: "operational_source_model_not_approved"}} : {}),
      encounterType: "office_established", placeOfService: "11", submittedEm: "99213",
      signedNote: note, transcript: "", intake: "", ...(headers ? {headerSource: pin} : {})};
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
    capabilities: {billingHeader: headers, providerHandoff: true}, persona: "Synthetic reviewer",
  }}));
  await page.route("**/api/encounters", (route) => route.fulfill({json: Object.values(details)}));
  await page.route("**/api/encounters/detail", (route) => route.fulfill({json: details[route.request().postDataJSON().encounterId]}));
  await page.route("**/api/completeness", (route) => route.fulfill({json: {complete: true, results: []}}));
  await page.route("**/api/encounters/coder-view", (route) => {
    coderRequests += 1;
    const {encounterId} = route.request().postDataJSON();
    return route.fulfill({json: {encounterId, encounterVersion: 1, readOnly: true, dataClass: source,
      reviewContext: "synthetic-browser-context", submittedEm: "99213",
      em: {kind: "EM", code: "99213", why: "Authored fixture", evidence: "Synthetic",
        evidenceSpans: [{source: "signed_note", text: "Synthetic"}]},
      dx: [], withheld: [], completeness: {complete: true, results: []}}});
  });
  await page.route(/\/api\/encounters\/header(?:\/save)?$/, async (route) => {
    const body = route.request().postDataJSON(), id = body.encounterId;
    expect(body.expectedSource).toEqual(details[id].headerSource);
    if (!new URL(route.request().url()).pathname.endsWith("/save")) {
      if (holdRead && id === ids[0] && !readHeld) {
        readHeld = true;
        await new Promise((resolve) => { releaseRead = resolve; });
      }
      return route.fulfill({json: {view: views[id]}});
    }
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
    if (holdSave) await new Promise((resolve) => { releaseSave = resolve; });
    return route.fulfill(lostSave ? {status: 503, json: {}} : {json: {view: views[id], created: true}});
  });
  await page.goto("/");
  await page.getByRole("button", {name: ids[0], exact: true}).click();
  const header = page.getByRole("region", {name: "Billing header", exact: true});
  if (!editHeader) return {header, views, navigation: page.getByRole("navigation", {name: "Workspace"})};
  if (holdRead) {
    await expect(header).toContainText("Loading header…");
    await expect.poll(() => typeof releaseRead).toBe("function");
    return {header, releaseRead: () => releaseRead(), navigation: page.getByRole("navigation", {name: "Workspace"})};
  }
  await header.getByRole("button", {name: "Edit header", exact: true}).click();
  await header.getByLabel("Reviewer note (optional)").fill("Keep my unsaved review.");
  await header.getByLabel("Reason for edit (required)").fill("Compared the synthetic documentation.");
  return {header, commands, releaseSave: () => releaseSave(), coderRequests: () => coderRequests, navigation: page.getByRole("navigation", {name: "Workspace"}),
    prompt: page.getByRole("group", {name: "Confirm discarding header edits"})};
}

for (const headers of [true, false]) {
  test(`coder ${headers ? "header signoff" : "feedback"} deadline preserves exact retry even when a response body ignores abort`, async ({page}) => {
    await page.clock.install();
    const path = headers ? "/api/encounters/header/signoff" : "/api/feedback";
    // Hold the first parsed body indefinitely, including after AbortSignal fires.
    // This exercises request ownership independently of fetch abort behavior.
    await page.addInitScript(({path}) => {
      const original = window.fetch;
      let first = true;
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (new URL(args[0], location.href).pathname !== path || !first) return response;
        first = false;
        const body = await response.json();
        return {ok: response.ok, status: response.status, json: () => new Promise((resolve) => {
          window.releaseExpiredCoderBody = () => resolve(body);
        })};
      };
    }, {path});
    const {header, views, navigation} = await setup(page, {workspace: "coder", editHeader: false, headers});
    const requests = [];
    await page.route(`**${path}`, async (route) => {
      const body = route.request().postDataJSON();
      requests.push(route.request().postData());
      const feedback = headers ? body.feedback : body;
      const response = {ok: true, reviewId: feedback.review_id, replayed: requests.length > 1};
      if (headers) {
        const view = views[body.encounterId];
        view.signoff ||= {id: actor.id, reviewId: feedback.review_id, headerId: body.headerId,
          headerRevision: body.expectedRevision, expectedSource: body.expectedSource,
          actor: {...actor, role: "coder"}, createdAt: "2026-10-07T12:02:00Z",
          status: "recorded", recommendationDigest: "c".repeat(64),
          humanProposalDecisions: body.humanProposalDecisions, baselineDecisions: body.baselineDecisions,
          codeDecisions: feedback.decisions, reviewerNote: feedback.overall_note};
        Object.assign(response, {view, headerSignoff: view.signoff});
      }
      await route.fulfill({json: response});
    });
    const accept = page.getByRole("group", {name: "Decision for 99213"}).getByRole("button", {name: "Accept", exact: true});
    await accept.click();
    if (headers) await page.getByLabel("I reviewed this header’s facts, code proposals, evidence, and documentation warnings.").check();
    await page.getByRole("button", {name: headers ? "Record coder review and header" : "Save review and prepare preview", exact: true}).click();
    await expect.poll(() => requests.length).toBe(1);
    await expect.poll(() => page.evaluate(() => typeof window.releaseExpiredCoderBody)).toBe("function");
    await page.clock.fastForward(HEADER_REQUEST_TIMEOUT_MS + 1);
    await expect(page.getByRole("alert")).toContainText("The coder review request timed out.");
    const retry = page.getByRole("button", {name: "Retry exact saved review", exact: true});
    await expect(retry).toBeEnabled();
    await expect(accept).toBeDisabled();
    await expect(navigation.getByRole("button", {name: "Provider", exact: true})).toBeDisabled();
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toBeDisabled();
    if (headers) await expect(header.getByRole("button", {name: "Edit header", exact: true})).toBeDisabled();
    await retry.click();
    await expect(page.getByText("Review recorded · exact replay confirmed", {exact: true})).toBeVisible();
    expect(requests).toHaveLength(2);
    expect(requests[1]).toBe(requests[0]);
    await page.evaluate(() => window.releaseExpiredCoderBody());
    await page.clock.runFor(HEADER_REQUEST_TIMEOUT_MS + 1);
    await expect(page.getByText("Review recorded · exact replay confirmed", {exact: true})).toBeVisible();
    await expect(page.getByText("Review recorded · first write confirmed", {exact: true})).toHaveCount(0);
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(navigation.getByRole("button", {name: "Provider", exact: true})).toBeEnabled();
  });
}

for (const workspace of ["provider", "coder"]) {
  test(`${workspace} timed-out save retains exact retry and rejects its late response`, async ({page}) => {
    await page.clock.install();
    const {header, navigation, commands, releaseSave} = await setup(page, {workspace, lostSave: true, holdSave: true});
    await header.getByRole("button", {name: "Save header edits", exact: true}).click();
    await expect.poll(() => commands.length).toBe(1);
    await page.clock.fastForward(HEADER_REQUEST_TIMEOUT_MS + 1);
    await expect(header).toContainText("The header request timed out. Its outcome is unknown.");
    await expect(header.getByRole("button", {name: "Retry same request", exact: true})).toBeEnabled();
    await expect(navigation.getByRole("button", {name: "Card chase", exact: true})).toBeDisabled();
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toBeDisabled();
    await header.getByRole("button", {name: "Retry same request", exact: true}).click();
    await expect(header).toContainText("Header edits saved");
    expect(commands).toHaveLength(2);
    expect(commands[1]).toEqual(commands[0]);
    await header.getByRole("button", {name: "Edit header", exact: true}).click();
    await header.getByLabel("Reviewer note (optional)").fill("Newer unsaved edit after confirmed retry.");
    releaseSave();
    await page.clock.runFor(100);
    await expect(header.getByLabel("Reviewer note (optional)")).toHaveValue("Newer unsaved edit after confirmed retry.");
    await expect(header.getByRole("button", {name: "Retry same request", exact: true})).toHaveCount(0);
    await expect(navigation.getByRole("button", {name: "Card chase", exact: true})).toBeEnabled();
  });

  test(`${workspace} timed-out read offers reload without blocking navigation`, async ({page}) => {
    await page.clock.install();
    const {header, navigation, releaseRead} = await setup(page, {workspace, holdRead: true});
    await page.clock.fastForward(HEADER_REQUEST_TIMEOUT_MS + 1);
    await expect(header).toContainText("Loading the header timed out.");
    await expect(navigation.getByRole("button", {name: "Card chase", exact: true})).toBeEnabled();
    await header.getByRole("button", {name: "Reload header", exact: true}).click();
    await expect(header.getByRole("button", {name: "Edit header", exact: true})).toBeVisible();
    await header.getByRole("button", {name: "Edit header", exact: true}).click();
    await header.getByLabel("Reviewer note (optional)").fill("Keep this edit after the replacement read.");
    releaseRead();
    await page.clock.runFor(100);
    await expect(header.getByLabel("Reviewer note (optional)")).toHaveValue("Keep this edit after the replacement read.");
    await expect(header.getByRole("button", {name: "Reload header", exact: true})).toHaveCount(0);
  });

  test(`${workspace} denied-model stored header preserves navigation and exact retry without inference`, async ({page}) => {
    const {header, prompt, navigation, commands, coderRequests} = await setup(page, {workspace, lostSave: true, operationalDenied: true});
    expect(coderRequests()).toBe(0);
    await page.getByRole("button", {name: ids[1], exact: true}).click();
    await expect(prompt).toBeVisible();
    await prompt.getByRole("button", {name: "Keep editing", exact: true}).click();
    await expect(header.getByLabel("Reviewer note (optional)")).toHaveValue("Keep my unsaved review.");
    await header.getByRole("button", {name: "Save header edits", exact: true}).click();
    await expect(header.getByRole("button", {name: "Retry same request", exact: true})).toBeVisible();
    await expect(navigation.getByRole("button", {name: "Card chase", exact: true})).toBeDisabled();
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toBeDisabled();
    await header.getByRole("button", {name: "Retry same request", exact: true}).click();
    await expect(header).toContainText("Header edits saved");
    expect(commands).toHaveLength(2);expect(commands[0]).toEqual(commands[1]);
    expect(coderRequests()).toBe(0);
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toBeEnabled();
  });

  test(`${workspace} can leave an unanswered read without locking the workspace`, async ({page}) => {
    const {header, navigation, releaseRead} = await setup(page, {workspace, holdRead: true});
    await expect(navigation.getByRole("button", {name: "Card chase", exact: true})).toBeEnabled();
    expect(await page.evaluate(() => {
      const event = new Event("beforeunload", {cancelable: true});
      window.dispatchEvent(event); return event.defaultPrevented;
    })).toBe(false);
    await page.getByRole("button", {name: ids[1], exact: true}).click();
    await expect(header.getByRole("button", {name: "Edit header", exact: true})).toBeVisible();
    releaseRead();
    await expect(page.getByRole("button", {name: ids[1], exact: true})).toHaveAttribute("aria-current", "true");
    await expect(page.getByRole("group", {name: "Confirm discarding header edits"})).toHaveCount(0);
  });

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
