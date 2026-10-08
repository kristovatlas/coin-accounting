// M3's graph (PLAN §4): open a transaction, flag it as a mix, follow an output to its spender, and
// re-label its address from the side panel; each change lands in the change log. The production build
// under the real CSP, and every image the graph view requests is the bundle's own (ADR 0037).
// Synthetic regtest data only: the harness mines to a public test address.
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

import { MINED_TO, startApp, stop, watchCsp } from "./app";

test("a transaction opens in the graph, and its flag and its output's tag are saved and logged", async ({ page }) => {
  const violations = await watchCsp(page);
  const images: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "image") images.push(request.url());
  });
  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");

    // An address with history: the miner's, in a wallet of its own, held by a wallet app. Both are
    // made after the graph view loaded, so its lists must catch up.
    await page.locator("#new-wallet-name").fill("Miner");
    await page.locator("#new-wallet").click();
    await expect(page.locator("#import-account option")).toHaveText(["Miner"]);
    await page.locator("#new-client-name").fill("Phone");
    await page.locator("#new-client").click();
    await page.locator(".import-client").first().check();
    await page.locator("#import-text").fill(MINED_TO);
    await page.locator("#preview-button").click();
    await page.locator("#import-button").click();
    await expect(page.locator("#import-result")).toContainText("Imported 1 new address.");
    const row = page.locator("#holdings-addresses tbody tr", { hasText: MINED_TO });
    await expect(async () => {
      await page.locator("#holdings-refresh").click();
      await expect(row.locator(".scan")).toHaveText("scanned", { timeout: 2_000 });
    }).toPass({ timeout: 60_000 });

    const outpoint = (await page.locator("#holdings-utxos .txid").first().textContent())?.trim() ?? "";
    const [txid, vout] = outpoint.split(":");
    expect(txid).toMatch(/^[0-9a-f]{64}$/);

    // Open it: the transaction is selected, and its outputs are listed. The note says what the graph
    // can't show (PLAN §4), and Cytoscape added no inline <style> (the CSP check below, ADR 0037).
    await expect(page.locator("#graph-known-links")).toContainText("known links only");
    await page.locator("#graph-txid").fill(txid);
    await page.locator("#graph-open").click();
    await expect(page.locator("#graph-panel-txid")).toHaveText(txid);
    const output = page.locator(`#graph-nodes [data-node="out:${txid}:${vout}"]`);
    await expect(output).toHaveCount(1);

    // The mixing flag is saved and logged.
    await page.locator("#graph-mixing").check();
    await page.locator("#graph-mixing-save").click();
    await expect(page.locator("#graph-mixing-result")).toHaveText("Marked as a mixing transaction.");
    await page.locator("#graph-show-history").click();
    await expect(page.locator("#graph-history li")).toHaveCount(1);

    // The miner's output: the user's, unspent.
    await output.click();
    await expect(page.locator("#graph-panel-owner")).toHaveText("Owner: You");
    await expect(page.locator("#tag-account option:checked")).toHaveText("Miner");
    await expect(page.locator(".tag-client").first()).toBeChecked(); // the import's wallet app
    await page.locator("#graph-spender").click();
    await expect(page.locator("#graph-spender-state")).toHaveText("Unspent as of block 101");

    // A new label for its address is saved and logged.
    await page.locator("#tag-label").fill("coinbase");
    await page.locator("#tag-save").click();
    await expect(page.locator("#tag-result")).toHaveText("Tag saved.");
    await expect(page.locator("#graph-panel-owner")).toHaveText("Owner: You (coinbase)");
    await page.locator("#tag-history").click();
    await expect(page.locator("#graph-history li")).toHaveCount(1);
    await expect(page.locator("#graph-history li")).not.toContainText('"client_ids":[]'); // the link was kept
    await expect(page.locator(".tag-client").first()).toBeChecked();
    expect(await page.locator("style").count()).toBe(0);

    const origin = `http://127.0.0.1:${app.port}/assets/`;
    for (const url of images) expect(url.startsWith(origin), url).toBe(true);
    expect(await violations()).toEqual([]);
  } finally {
    await stop(app);
  }
});
