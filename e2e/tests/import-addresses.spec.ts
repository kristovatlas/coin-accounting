// M2's import flow (PLAN §3; THREAT_MODEL T-701, T-703): add a wallet, paste an address list, see the
// preview (including the line that isn't an address), import it, and see a private key refused without
// being repeated. The production build under the real CSP; any violation fails the test. Synthetic
// regtest addresses only.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

const python = process.env.COINACCT_E2E_PYTHON;
const root = resolve(__dirname, "..", "..");

// BIP173-style regtest P2WPKH addresses for synthetic programs 7001 and 7002.
const FIRST = "bcrt1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqx6evpqw46";
const SECOND = "bcrt1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqx66zj4cm9";
// Shaped like a WIF private key (a Base58 alphabet slice), not a real key.
const WIF_SHAPED = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz").slice(0, 52);

type Started = { proc: ChildProcessWithoutNullStreams; launchFile: string; port: number };

async function startApp(): Promise<Started> {
  if (!python) throw new Error("COINACCT_E2E_PYTHON is not set: run the E2E tests with `make e2e`");
  const workdir = mkdtempSync(join(tmpdir(), "coinacct-e2e-"));
  const proc = spawn(python, ["-m", "harness.app_under_test", workdir], {
    cwd: join(root, "e2e"),
    env: {
      PATH: "/usr/bin:/bin",
      HOME: workdir,
      PYTHONPATH: [join(root, "e2e"), join(root, "backend")].join(":"),
      PYTHONDONTWRITEBYTECODE: "1",
    },
  });
  const line: string = await new Promise((done, fail) => {
    const lines = createInterface({ input: proc.stdout });
    lines.once("line", done);
    proc.once("exit", (code) => fail(new Error(`the harness exited (${code}) before the app served`)));
  });
  const info = JSON.parse(line) as { launch_file?: string; port?: number; error?: string };
  if (!info.launch_file || !info.port) throw new Error(info.error ?? `unexpected harness output: ${line}`);
  return { proc, launchFile: info.launch_file, port: info.port };
}

test("an address list is previewed, imported, and a private key is refused unrepeated", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && /Content Security Policy/i.test(msg.text())) violations.push(msg.text());
  });

  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");

    // A wallet to import into.
    await page.locator("#new-wallet-name").fill("Cold storage");
    await page.locator("#new-wallet").click();
    await expect(page.locator("#import-account")).toHaveValue(/\d+/);
    await expect(page.locator("#import-account option")).toHaveText(["Cold storage"]);

    // Preview: two addresses, one line that isn't an address (reported by number), nothing written yet.
    await page.locator("#import-text").fill(`${FIRST}\nnot an address\n${SECOND}\n`);
    await expect(page.locator("#import-button")).toBeDisabled(); // only a previewed upload can be imported
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveText([FIRST, SECOND]);
    await expect(page.locator("#preview-invalid")).toContainText("line 2");

    // Import what the preview showed.
    await page.locator("#import-button").click();
    await expect(page.locator("#import-result")).toHaveText("Imported 2 addresses. They are being scanned.");
    await expect(page.locator("#import-text")).toHaveValue("");

    // The same list again: both are now known, and stay with their account.
    await page.locator("#import-text").fill(`${FIRST}\n${SECOND}`);
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveCount(0);
    await expect(page.locator("#preview-known li")).toHaveCount(2);

    // A private key anywhere refuses the upload, and the page never shows it back (T-703).
    await page.locator("#import-text").fill(`${FIRST}\n${WIF_SHAPED}`);
    await page.locator("#preview-button").click();
    await expect(page.locator("#import-error")).toContainText("private key");
    await expect(page.locator("#preview")).toHaveCount(0);
    await expect(page.locator("#import-error")).not.toContainText(WIF_SHAPED.slice(1, 20));
  } finally {
    if (app.proc.exitCode === null) app.proc.kill("SIGTERM");
  }
  expect(violations).toEqual([]);
});
