import { test as base, expect } from "@playwright/test";

// Synthetic HTTP contract fixtures exercise the actual built React application.
// They grant no backend capability and cannot contact a vendor.
export const workspaceRows = ["A", "B"].map((name) => ({ chaseId: `SYN-SMS-${name}`,
      patientLabel: `Synthetic patient ${name}`, state: "in_progress", bucket: "missing",
      priority: 1, touches: 0, appointmentAt: null }));

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const origin = "http://127.0.0.1:" + (process.env.COSMANEURA_E2E_BASE_PORT || "8765");
    const rows = structuredClone(workspaceRows);
    const outside = [], errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) { outside.push(url.origin); return route.abort(); }
      if (!url.pathname.startsWith("/api/")) return route.continue();
      let json;
      if (url.pathname === "/api/health") json = { ok: true };
      if (url.pathname === "/api/session") json = { views: ["chase"], defaultView: "chase",
        capabilities: { manageChase: true } };
      if (url.pathname === "/api/chase/worklist") json = { rows, counts: { open: 2,
        needs_person: 0, unassigned: 0, by_bucket: { missing: 2, expired: 0, eligibility: 0 } } };
      if (url.pathname === "/api/chase/task") {
        const row = rows.find((entry) => entry.chaseId === route.request().postDataJSON().chaseId);
        if (row) json = { ...row, open: true, assigned: true, allowedActions: [], contact: null,
          instruction: "Review the synthetic text conversation.",
          systemOfRecord: { name: "Synthetic training", instruction: "No practice-record link." },
          communications: { available: true, mode: "durable" } };
      }
      return route.fulfill({ status: json ? 200 : 404, json: json || {} });
    });
    await use(page);
    if (testInfo.title.startsWith("SMS drafts save") && testInfo.status === "passed") {
      await page.screenshot({ path: testInfo.outputPath("sms-desktop.png"), fullPage: true });
      await page.setViewportSize({ width: 900, height: 1100 });
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath("sms-tablet.png"), fullPage: true });
    }
    expect(outside, "browser requests must remain on the fixture origin").toEqual([]);
    expect(errors, "built application must not raise browser errors").toEqual([]);
  },
});
export { expect };
