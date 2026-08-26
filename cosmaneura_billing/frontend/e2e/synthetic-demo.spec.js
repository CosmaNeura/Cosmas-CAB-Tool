import { expect, test } from "@playwright/test";

const configuredBasePort = Number.parseInt(
  process.env.COSMANEURA_E2E_BASE_PORT || "8765",
  10,
);
const origins = Object.freeze({
  admin: `http://127.0.0.1:${configuredBasePort}`,
  provider: `http://127.0.0.1:${configuredBasePort + 1}`,
  coder: `http://127.0.0.1:${configuredBasePort + 2}`,
  billing: `http://127.0.0.1:${configuredBasePort + 3}`,
});

const COMPLETE_TELEHEALTH_NOTE = [
  "This visit was conducted via two-way audio-video telehealth.",
  "Patient verbally consented to this telehealth visit.",
  "Patient was at home in Nashua, NH.",
  "Provider was at the office in Bow, NH.",
  "Total time spent on the date of the encounter: 25 minutes.",
].join(" ");

async function openWorkspace(page, origin = origins.admin) {
  await page.goto(origin);
  const safety = page.getByRole("region", { name: "Synthetic pilot safety status" });
  await expect(safety).toBeVisible();
  await expect(safety).toContainText("Synthetic data");
  await expect(safety).toContainText("Human review required");
  await expect(safety).toContainText("No EHR or claim transmission");
  await expect(safety).toContainText("Patient outreach disabled by demo policy");
  return safety;
}

async function selectEncounter(page, encounterId) {
  const queue = page.getByRole("region", { name: "Scrollable encounter work queue" });
  await expect(queue).toBeVisible();
  await queue.getByRole("button", { name: encounterId, exact: true }).click();
}

async function openCoderReview(page) {
  await openWorkspace(page);
  await page.getByRole("button", { name: "Coder review", exact: true }).click();
  await selectEncounter(page, "ENC-100237");
  await expect(page.getByRole("heading", { name: "Coder review · ENC-100237" })).toBeVisible();
}

async function acceptEveryEngineSuggestion(page) {
  const groups = page.getByRole("group", { name: /^Decision for / });
  await expect(groups.first()).toBeVisible();
  const count = await groups.count();
  expect(count).toBeGreaterThan(0);
  for (let index = 0; index < count; index += 1) {
    await groups.nth(index).getByRole("button", { name: "Accept", exact: true }).click();
  }
}

test("provider demo renders evidence, exclusions, completeness, and safe copy actions", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: origins.admin,
  });
  await openWorkspace(page);

  await expect(page.getByTitle("Server-assigned pilot persona")).toHaveText("Role · Administrator");
  await expect(page.getByRole("button", { name: "Provider", exact: true })).toHaveAttribute("aria-current", "page");
  await selectEncounter(page, "ENC-100237");
  await expect(page.getByRole("heading", { name: "Provider Billing Review" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Documentation completeness" }).locator(".."))
    .toContainText("All completeness requirements were found");

  await page.getByRole("button", { name: "Run billing review" }).click();
  const emReview = page.locator('section[aria-labelledby="em-heading"]');
  await expect(emReview).toContainText("Provider selected");
  await expect(emReview).toContainText("99213");
  await expect(emReview).toContainText("Evidence-supported");
  await expect(emReview).toContainText("99214");

  const held = page.locator("article").filter({
    has: page.locator("code", { hasText: "E66.8" }),
  });
  const removed = page.locator("article").filter({
    has: page.locator("code", { hasText: "Z80.0" }),
  });
  await expect(held).toContainText("Held:");
  await expect(held).toContainText("not addressed");
  await expect(removed).toContainText("Removed:");
  await expect(removed).toContainText("denied");

  const evidenceSummary = held.locator("summary").filter({ hasText: "Evidence from" });
  await evidenceSummary.click();
  await expect(held.locator("blockquote")).toContainText("weigh 255 pounds");

  await page.getByRole("button", { name: "Copy suggested code K62.5" }).click();
  await expect(page.getByRole("status")).toContainText("Code K62.5 copied — not sent.");
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe("K62.5");

  await page.getByRole("button", { name: "Copy suggested codes" }).click();
  await expect(page.getByRole("status")).toContainText("Suggested codes copied — not sent.");
  const copiedCodes = (await page.evaluate(() => navigator.clipboard.readText())).split(/\s+/);
  expect(copiedCodes).toEqual(expect.arrayContaining(["99214", "K62.5"]));
  expect(copiedCodes).not.toContain("E66.8");
  expect(copiedCodes).not.toContain("Z80.0");

  await page.getByRole("button", { name: "Copy prepared Athena-friendly block" }).click();
  await expect(page.getByRole("status")).toContainText("copied — not sent");
  await expect(page.getByText("No", { exact: true }).last()).toBeVisible();
  await expect(page.getByText(/Prepared billing guidance only/)).toContainText("no EHR, Athena, clearinghouse, payer, or patient transmission occurred");
});

test("completeness passes, blocks, and records a reasoned override without hiding the finding", async ({ page, request }) => {
  const passed = await request.post("/api/completeness", {
    data: { note: COMPLETE_TELEHEALTH_NOTE, placeOfService: "10" },
  });
  expect(passed.status()).toBe(200);
  const passedBody = await passed.json();
  expect(passedBody.complete).toBe(true);
  expect(passedBody.attestation_block).toContain("Billing attestation");

  const blocked = await request.post("/api/completeness", {
    data: { note: "Telehealth visit.", placeOfService: "10" },
  });
  expect(blocked.status()).toBe(200);
  const blockedBody = await blocked.json();
  expect(blockedBody.complete).toBe(false);
  expect(blockedBody.attestation_block).toBeNull();

  const withoutConsent = COMPLETE_TELEHEALTH_NOTE.replace(
    "Patient verbally consented to this telehealth visit. ",
    "",
  );
  const overridden = await request.post("/api/completeness", {
    data: {
      note: withoutConsent,
      placeOfService: "10",
      overrides: {
        "telehealth.consent": "standing synthetic annual consent is documented separately",
      },
    },
  });
  expect(overridden.status()).toBe(200);
  const overriddenBody = await overridden.json();
  const consent = overriddenBody.results.find((item) => item.rule_id === "telehealth.consent");
  expect(overriddenBody.complete).toBe(true);
  expect(consent.status).toBe("missing");
  expect(consent.provider_override).toEqual({
    overridden: true,
    reason: "standing synthetic annual consent is documented separately",
  });
  expect(overriddenBody.attestation_block).toContain("provider override");

  await openWorkspace(page);
  await selectEncounter(page, "SYN-RC-0003");
  const completeness = page.getByRole("heading", { name: "Documentation completeness" }).locator("..");
  await expect(completeness).toContainText("Documentation completeness blocks a ready state");
  await expect(completeness).toContainText("telehealth.duration");
  await expect(completeness).toContainText("telehealth.patient_location");
  await expect(completeness.getByText("View completeness attestation")).toHaveCount(0);

  await page.getByRole("button", { name: "Coder review", exact: true }).click();
  await selectEncounter(page, "ENC-100237");
  await expect(page.getByRole("heading", { name: "Coder review · ENC-100237" })).toBeVisible();
  await expect(page.getByRole("row", { name: /SYN-RC-0003/ })).not.toContainText("Coder load failed");
});

test("coder requires explicit decisions and safely replays an exact lost response", async ({ page }) => {
  await openCoderReview(page);
  await expect(page.getByText("unreviewed", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "Add code", exact: true }).click();
  const manual = page.locator('section[aria-labelledby="manual-heading"]');
  await manual.getByLabel("Code", { exact: true }).fill("Z71.1");
  await manual.getByLabel("Rationale").fill("Synthetic acceptance-test rationale");
  await page.getByRole("button", { name: "Save review and prepare preview" }).click();
  const validationAlert = page.getByRole("alert").filter({
    hasText: "Complete the review before saving",
  });
  await expect(validationAlert.getByText(
    "Add supporting documentation evidence.",
    { exact: true },
  )).toBeVisible();
  await manual.getByRole("button", { name: "Remove human-added code 1" }).click();

  await acceptEveryEngineSuggestion(page);
  let feedbackCalls = 0;
  let firstPayload;
  let replayPayload;
  let firstServerStatus;
  await page.route("**/api/feedback", async (route) => {
    feedbackCalls += 1;
    const payload = route.request().postDataJSON();
    if (feedbackCalls === 1) {
      firstPayload = payload;
      const serverResponse = await route.fetch();
      firstServerStatus = serverResponse.status();
      await serverResponse.body();
      await route.abort("failed");
      return;
    }
    replayPayload = payload;
    await route.continue();
  });

  await page.getByRole("button", { name: "Save review and prepare preview" }).click();
  await expect(page.getByText(/The server outcome is unknown/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry exact saved review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Provider", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Card chase", exact: true })).toBeDisabled();
  expect(firstServerStatus).toBe(200);

  await page.getByRole("button", { name: "Retry exact saved review" }).click();
  const receipt = page.getByRole("status").filter({ hasText: "Review recorded" });
  await expect(receipt).toContainText("exact replay confirmed");
  await expect(page.getByRole("heading", { name: "Prepared draft — not transmitted" })).toBeVisible();
  await expect(page.getByText(/No claim, EHR update, Athena write/)).toBeVisible();
  expect(feedbackCalls).toBe(2);
  expect(replayPayload).toEqual(firstPayload);
});

test("stale review transport fails closed while preserving the frozen draft", async ({ page }) => {
  await openCoderReview(page);
  await acceptEveryEngineSuggestion(page);

  await page.route("**/api/feedback", async (route) => {
    const stale = route.request().postDataJSON();
    stale.encounter_version += 1;
    const headers = { ...route.request().headers() };
    delete headers["content-length"];
    await route.continue({
      headers,
      postData: JSON.stringify(stale),
    });
  });
  const staleResponse = page.waitForResponse((response) => (
    response.url().endsWith("/api/feedback") && response.status() === 409
  ));

  await page.getByRole("button", { name: "Save review and prepare preview" }).click();
  expect((await staleResponse).status()).toBe(409);
  await expect(page.getByText(/encounter or review version changed/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Reload after confirmed conflict" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Prepared draft — not transmitted" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Provider", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Card chase", exact: true })).toBeDisabled();
  await expect(page.getByText(/nothing was transmitted/i)).toBeVisible();
});

test("Card Chase holds the direct-contract case and records one manual task without outreach", async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole("button", { name: "Card chase", exact: true }).click();
  await expect(page.getByText(/Outbound automation is off; transport is record_only/)).toBeVisible();
  await expect(page.getByText(/never sends patient messages/)).toBeVisible();

  await page.getByRole("button", { name: /Synthetic patient D/ }).click();
  await expect(page.getByText("Contact destination withheld", { exact: true })).toBeVisible();
  await expect(page.getByText(/patient is on the direct-contract list/).last()).toBeVisible();
  await expect(page.getByRole("button", { name: "Record attempt" })).toHaveCount(0);

  await page.getByRole("button", { name: /Synthetic patient A/ }).click();
  await expect(page.getByText("Synthetic manual-call destination")).toBeVisible();
  await expect(page.getByText(/does not dial, text, email, or open an external link/)).toBeVisible();
  let attemptCalls = 0;
  let firstAttempt;
  let replayedAttempt;
  let firstAttemptStatus;
  await page.route("**/api/chase/action", async (route) => {
    const payload = route.request().postDataJSON();
    if (payload.action !== "log_attempt") {
      await route.continue();
      return;
    }
    attemptCalls += 1;
    if (attemptCalls === 1) {
      firstAttempt = payload;
      const serverResponse = await route.fetch();
      firstAttemptStatus = serverResponse.status();
      await serverResponse.body();
      await route.abort("failed");
      return;
    }
    replayedAttempt = payload;
    await route.continue();
  });
  await page.getByLabel("Minutes").fill("2");
  await page.getByLabel("Note (optional)").fill("Synthetic manual workflow acceptance test");
  await page.getByRole("button", { name: "Record attempt" }).click();
  await expect(page.getByText("The action could not be confirmed. No automatic contact was sent.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry the same action safely" })).toBeVisible();
  expect(firstAttemptStatus).toBe(200);
  await page.getByRole("button", { name: "Retry the same action safely" }).click();
  await expect(page.getByRole("status")).toContainText("Manual attempt recorded");
  expect(attemptCalls).toBe(2);
  expect(replayedAttempt).toEqual(firstAttempt);

  await page.getByRole("button", { name: "Mark submitted" }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByRole("button", { name: "Verify and close" })).toBeVisible();
  await page.getByRole("button", { name: "Verify and close" }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("This task is closed. No additional patient contact is needed.")).toBeVisible();
  await expect(page.getByText(/no EHR, Athena, clearinghouse, payer, or patient transmission occurred/)).toBeVisible();
});

test("server-owned personas constrain tabs and deny direct role violations", async ({ browser }) => {
  const page = await browser.newPage();

  await openWorkspace(page, origins.provider);
  await expect(page.getByTitle("Server-assigned pilot persona")).toHaveText("Role · Provider");
  await expect(page.getByRole("button", { name: "Provider", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Coder review", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Card chase", exact: true })).toHaveCount(0);
  const providerCoderDenial = await page.request.post(`${origins.provider}/api/encounters/coder-view`, {
    data: { encounterId: "ENC-100237" },
  });
  expect(providerCoderDenial.status()).toBe(403);
  const providerChaseDenial = await page.request.post(`${origins.provider}/api/chase/worklist`, {
    data: {},
  });
  expect(providerChaseDenial.status()).toBe(403);

  await openWorkspace(page, origins.billing);
  await expect(page.getByTitle("Server-assigned pilot persona")).toHaveText("Role · Billing staff");
  await expect(page.getByRole("button", { name: "Card chase", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Provider", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Coder review", exact: true })).toHaveCount(0);
  const billingProviderDenial = await page.request.post(`${origins.billing}/api/encounters/detail`, {
    data: { encounterId: "ENC-100237" },
  });
  expect(billingProviderDenial.status()).toBe(403);

  await openWorkspace(page, origins.coder);
  await expect(page.getByTitle("Server-assigned pilot persona")).toHaveText("Role · Medical coder");
  await expect(page.getByRole("button", { name: "Provider", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Coder review", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Card chase", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Card chase", exact: true }).click();
  await page.getByRole("button", { name: /Synthetic patient A/ }).click();
  await expect(page.getByText("This worklist is read-only for your assigned role.")).toBeVisible();
  const coderWorklist = await page.request.post(`${origins.coder}/api/chase/worklist`, { data: {} });
  expect(coderWorklist.status()).toBe(200);
  const coderRow = (await coderWorklist.json()).rows[0];
  const coderMutationDenial = await page.request.post(`${origins.coder}/api/chase/action`, {
    data: {
      chaseId: coderRow.chaseId,
      action_id: "11111111-1111-4111-8111-111111111111",
      action: "assign",
    },
  });
  expect(coderMutationDenial.status()).toBe(403);

  const adHocDenial = await page.request.post(`${origins.admin}/api/billing`, { data: {} });
  expect(adHocDenial.status()).toBe(403);
  await page.close();
});

test("API outage shows an honest failure with no browser fallback", async ({ page }) => {
  await openWorkspace(page);
  await selectEncounter(page, "ENC-100237");
  await page.route("**/api/encounters/billing-tab", (route) => route.abort("failed"));
  await page.getByRole("button", { name: "Run billing review" }).click();
  await expect(page.getByRole("alert").filter({
    hasText: "No browser fallback or canned result was substituted.",
  })).toContainText(
    "No browser fallback or canned result was substituted.",
  );
  await expect(page.getByRole("heading", { name: "Human-controlled handoff" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Copy suggested codes" })).toHaveCount(0);
});

test("common tablet width keeps the page contained and the queue keyboard-operable", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await openWorkspace(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const queue = page.getByRole("region", { name: "Scrollable encounter work queue" });
  await queue.focus();
  await page.keyboard.press("Tab");
  const encounter = queue.getByRole("button", { name: "ENC-100237", exact: true });
  await expect(encounter).toBeFocused();
  expect(await encounter.evaluate((element) => element.matches(":focus-visible"))).toBe(true);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Provider Billing Review" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
