// E2E against the production build under the real CSP (ENGINEERING §3.1). The browser is the pinned,
// hash-verified Chrome for Testing headless shell (`make e2e-tools`), launched by path: Playwright's
// own browser downloader never runs (ENGINEERING §2.3). `make e2e` sets both variables.
import { defineConfig } from "@playwright/test";

const chrome = process.env.COINACCT_E2E_CHROME;
if (!chrome) {
  throw new Error("COINACCT_E2E_CHROME is not set: run the E2E tests with `make e2e`");
}

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  timeout: 120_000,
  reporter: [["list"]],
  use: {
    headless: true,
    launchOptions: { executablePath: chrome },
    trace: "off",
    screenshot: "off",
    video: "off",
  },
});
