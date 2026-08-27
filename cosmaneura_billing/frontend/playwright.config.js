import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(frontendRoot, "..");
const configuredBasePort = Number.parseInt(
  process.env.COSMANEURA_E2E_BASE_PORT || "8765",
  10,
);

if (!Number.isInteger(configuredBasePort)
    || configuredBasePort < 1024
    || configuredBasePort > 65532) {
  throw new Error("COSMANEURA_E2E_BASE_PORT must leave four ports in the 1024-65535 range");
}

const virtualenvPython = path.resolve(
  projectRoot,
  process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python",
);
const python = JSON.stringify(
  process.env.COSMANEURA_E2E_PYTHON
    || (existsSync(virtualenvPython)
      ? virtualenvPython
      : process.platform === "win32" ? "python" : "python3"),
);
const replaceCommandShell = process.platform === "win32" ? "" : "exec ";
const personaServers = [
  ["admin", configuredBasePort],
  ["provider", configuredBasePort + 1],
  ["coder", configuredBasePort + 2],
  ["billing-staff", configuredBasePort + 3],
];

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: "line",
  outputDir: "test-results",
  use: {
    baseURL: `http://127.0.0.1:${configuredBasePort}`,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [{
    name: "chromium",
    use: { ...devices["Desktop Chrome"] },
  }],
  webServer: personaServers.map(([persona, port]) => ({
    command: `${replaceCommandShell}${python} -m cosmaneura_billing.local_demo --port ${port} --persona ${persona}`,
    cwd: projectRoot,
    url: `http://127.0.0.1:${port}/api/ready`,
    reuseExistingServer: false,
    timeout: 180_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  })),
});
