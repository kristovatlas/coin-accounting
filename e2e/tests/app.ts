// Shared E2E helpers (not a spec): start the app under test against regtest, stop it and remove its
// workdir, and collect CSP violations (ENGINEERING §3.1). Synthetic regtest data only.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";

import type { Page } from "@playwright/test";

const python = process.env.COINACCT_E2E_PYTHON;
const root = resolve(__dirname, "..", "..");

export type Started = { proc: ChildProcessWithoutNullStreams; launchFile: string; port: number; workdir: string };

/** The harness's regtest node mines its first 101 blocks to this public test address
 * (P2WSH(OP_TRUE), e2e/harness/regtest.py): 101 coinbase outputs of 50 BTC each. */
export const MINED_TO = "bcrt1qft5p2uhsdcdc3l2ua4ap5qqfg4pjaqlp250x7us7a8qqhrxrxfsqseac85";

export async function startApp(): Promise<Started> {
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
  try {
    const line: string = await new Promise((done, fail) => {
      const lines = createInterface({ input: proc.stdout });
      lines.once("line", done);
      proc.once("exit", (code) => fail(new Error(`the harness exited (${code}) before the app served`)));
    });
    const info = JSON.parse(line) as { launch_file?: string; port?: number; error?: string };
    if (!info.launch_file || !info.port) throw new Error(info.error ?? `unexpected harness output: ${line}`);
    return { proc, launchFile: info.launch_file, port: info.port, workdir };
  } catch (error) {
    await stop({ proc, launchFile: "", port: 0, workdir });
    throw error;
  }
}

/** Stop the app (if it still runs) and remove its workdir, whose launch file held a bootstrap token. */
export async function stop(app: Started): Promise<void> {
  try {
    if (app.proc.exitCode === null && app.proc.signalCode === null) {
      const exited = new Promise((done) => app.proc.once("exit", done));
      app.proc.kill("SIGTERM");
      await Promise.race([exited, new Promise((done) => setTimeout(done, 15_000))]);
      if (app.proc.exitCode === null && app.proc.signalCode === null) app.proc.kill("SIGKILL");
    }
  } finally {
    rmSync(app.workdir, { recursive: true, force: true });
  }
}

/** Collects securitypolicyviolation events and CSP console errors; call before navigating. */
export async function watchCsp(page: Page): Promise<() => Promise<string[]>> {
  const logged: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && /Content Security Policy/i.test(msg.text())) logged.push(msg.text());
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      (window as unknown as { __csp: string[] }).__csp ??= [];
      (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  return async () => [
    ...logged,
    ...(await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? [])),
  ];
}
