import { defineConfig } from "@playwright/test";

const port = Number(process.env.COSMANEURA_E2E_BASE_PORT || "8765");
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Invalid local test port");
export default defineConfig({
  testDir: "./e2e", testMatch: ["communications-operational.spec.js", "communications-event-review.spec.js", "unassigned-texts.spec.js", "communications-count-refresh.spec.js", "athena-contact-workspace.spec.js"],
  workers: 1, retries: 0, timeout: 30_000,
  use: { baseURL: `http://127.0.0.1:${port}`, viewport: { width: 1440, height: 1080 },
    screenshot: "only-on-failure", launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {} },
  webServer: { command: `python3 -m http.server ${port} --bind 127.0.0.1 --directory dist`,
    url: `http://127.0.0.1:${port}`, reuseExistingServer: false, timeout: 10_000 },
});
