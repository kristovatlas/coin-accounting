// M2's history views (PLAN §3): import an address with real regtest history, let the chain jobs scan
// it, and see its balance, its outputs and its events, as of the last sync. The production build
// under the real CSP. Synthetic regtest data only: the harness mines to a public test address.
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

import { MINED_TO, startApp, stop, watchCsp } from "./app";

test("an imported address shows its scanned balance, outputs and history", async ({ page }) => {
  const violations = await watchCsp(page);
  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");

    await page.locator("#new-wallet-name").fill("Miner");
    await page.locator("#new-wallet").click();
    await expect(page.locator("#import-account option")).toHaveText(["Miner"]);
    await page.locator("#import-text").fill(MINED_TO);
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveText([MINED_TO]);
    await page.locator("#import-button").click();
    await expect(page.locator("#import-result")).toContainText("Imported 1 new address.");

    // The import asked the chain jobs for a sync; refresh until it has scanned the address.
    const row = page.locator("#holdings-addresses tbody tr", { hasText: MINED_TO });
    await expect(async () => {
      await page.locator("#holdings-refresh").click();
      await expect(row.locator(".scan")).toHaveText("scanned", { timeout: 2_000 });
    }).toPass({ timeout: 60_000 });

    // 101 coinbase outputs of 50 BTC each, all unspent.
    await expect(row.locator(".balance")).toHaveText("5050.00000000");
    await expect(page.locator("#holdings-total")).toHaveText("Total: 5050.00000000 BTC");
    await expect(page.locator("#holdings-utxos tr")).toHaveCount(101);
    await expect(page.locator("#holdings-as-of")).toContainText("As of block 101");

    await row.locator(".show-history").click();
    await expect(page.locator("#address-events tbody tr")).toHaveCount(101);
    await expect(page.locator("#address-events tbody tr").first()).toContainText("received");
    await expect(page.locator("#address-history-incomplete")).toHaveCount(0); // scanned through the sync

    // Refresh reloads the open history with the holdings, so both are from one snapshot (T-207).
    await page.locator("#holdings-refresh").click();
    await expect(page.locator("#address-history h3")).toHaveText(`History of ${MINED_TO}`);
    await expect(page.locator("#address-events tbody tr")).toHaveCount(101);
    expect(await violations()).toEqual([]);
  } finally {
    await stop(app);
  }
});
