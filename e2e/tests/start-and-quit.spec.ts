// The first user flow (M0.3): start the app against a regtest node, open the launch file as the
// browser would, see the node's status, and quit. Any CSP violation fails the test (ENGINEERING §3.1).
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

const python = process.env.COINACCT_E2E_PYTHON;
const root = resolve(__dirname, "..", "..");

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
      PYTHONDONTWRITEBYTECODE: "1", // the pinned Python's tree is verified file by file (Makefile)
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

test("the app starts online, shows the node's status and quits cleanly", async ({ page }) => {
  const violations: string[] = [];
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      (window as unknown as { __csp: string[] }).__csp ??= [];
      (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  page.on("console", (msg) => {
    if (msg.type() === "error" && /Content Security Policy/i.test(msg.text())) violations.push(msg.text());
  });

  // The placeholder page shows the same texts, so only these prove the production build is served.
  const scripts: string[] = [];
  page.on("response", (response) => {
    if (response.request().resourceType() === "script") scripts.push(`${response.status()} ${new URL(response.url()).pathname}`);
  });

  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page).toHaveURL(`http://127.0.0.1:${app.port}/`); // the token is gone from the address (T-110)
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");
    expect(await page.evaluate(() => window.sessionStorage.getItem("session"))).toBeTruthy();
    await expect(page.locator("#root > main")).toBeVisible(); // React rendered into the build's root
    expect(scripts.length).toBeGreaterThan(0);
    for (const script of scripts) expect(script).toMatch(/^200 \/assets\/[^/]+\.js$/); // Vite's hashed bundle
    expect(await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? [])).toEqual([]);

    const exited = new Promise<number | null>((done) => app.proc.once("exit", done));
    await page.locator("#quit").click();
    await expect(page.locator("#status")).toHaveText("Coin Accounting has stopped. You can close this tab.");
    expect(await exited).toBe(0);
  } finally {
    if (app.proc.exitCode === null) app.proc.kill("SIGTERM");
  }
  expect(violations).toEqual([]);
});
