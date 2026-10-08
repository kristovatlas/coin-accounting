// M2's import flow (PLAN §3; THREAT_MODEL T-701, T-703): add a wallet, paste an address list, see the
// preview (including the line that isn't an address), import it, and see a private key refused without
// being repeated. The production build under the real CSP; any violation fails the test. Synthetic
// regtest addresses only.
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

import { startApp, stop, watchCsp } from "./app";

// BIP173-style regtest P2WPKH addresses for synthetic programs 7001 and 7002.
const FIRST = "bcrt1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqx6evpqw46";
const SECOND = "bcrt1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqx66zj4cm9";
// The BIP32 test vector 1 seed's extended public key, re-encoded for testnet: public test data, nobody's
// wallet. Its first three receive addresses on regtest (checked against bitcoind's deriveaddresses).
const TPUB =
  "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1RhGjxbaTLV3X4FyWuejifB9jusQ46QzG87VKp";
const DERIVED = [
  "bcrt1qp5wfcq48h6d63wyy9qz0awtpfqwwv4sm4gc9mc",
  "bcrt1qrfxr69jqnhwufxgkqgcdep9prq4j4vuwzpxkrk",
  "bcrt1qhvd6suvqzjcu9pxjhrwhtrlj85ny3n2mg0a2z0",
];
// Shaped like a WIF private key (a Base58 alphabet slice), not a real key.
const WIF_SHAPED = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz").slice(0, 52);

test("an address list is previewed, imported, and a private key is refused unrepeated", async ({ page }) => {
  const violations = await watchCsp(page);
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

    // An edit after the preview withdraws it: only a previewed upload can be imported (T-701).
    await page.locator("#import-text").fill(`${FIRST}\nnot an address\n${SECOND}\nedited`);
    await expect(page.locator("#import-button")).toBeDisabled();
    await expect(page.locator("#preview")).toHaveCount(0);
    await page.locator("#import-text").fill(`${FIRST}\nnot an address\n${SECOND}\n`);
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveText([FIRST, SECOND]);

    // Import what the preview showed.
    await page.locator("#import-button").click();
    await expect(page.locator("#import-result")).toHaveText("Imported 2 new addresses. They are being scanned.");
    await expect(page.locator("#import-text")).toHaveValue("");

    // The same list again: both are now known, and stay with their account.
    await page.locator("#import-text").fill(`${FIRST}\n${SECOND}`);
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveCount(0);
    await expect(page.locator("#preview-known li")).toHaveText([
      `${FIRST} — already in Cold storage`,
      `${SECOND} — already in Cold storage`,
    ]);

    // A private key anywhere refuses the upload, and the page never shows it back (T-703).
    await page.locator("#import-text").fill(`${FIRST}\n${WIF_SHAPED}`);
    await page.locator("#preview-button").click();
    await expect(page.locator("#import-error")).toContainText("private key");
    await expect(page.locator("#preview")).toHaveCount(0);
    await expect(page.locator("#import-error")).not.toContainText(WIF_SHAPED.slice(1, 20));
    expect(await violations()).toEqual([]);
  } finally {
    await stop(app);
  }
});

test("a public descriptor is previewed through the node and imported", async ({ page }) => {
  const violations = await watchCsp(page);
  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");
    await page.locator("#new-wallet-name").fill("Descriptor wallet");
    await page.locator("#new-wallet").click();
    await expect(page.locator("#import-account option")).toHaveText(["Descriptor wallet"]);

    await page.locator("#kind-descriptor").check();
    await page.locator("#gap-limit").fill("3");
    await page.locator("#import-text").fill(`wpkh(${TPUB}/0/*)`);
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveText(DERIVED.map((a, i) => `${i}: ${a}`));
    await expect(page.locator("#preview")).toContainText("the first 3 addresses are scanned");

    // A different gap limit is a different import: the preview is withdrawn (T-701).
    await page.locator("#gap-limit").fill("5");
    await expect(page.locator("#preview")).toHaveCount(0);
    await expect(page.locator("#import-button")).toBeDisabled();
    await page.locator("#gap-limit").fill("3");
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-new li")).toHaveCount(3);

    await page.locator("#import-button").click();
    await expect(page.locator("#import-result")).toHaveText("Imported the descriptor. Its addresses are being scanned.");

    // Its addresses are now this wallet's.
    await page.locator("#kind-addresses").check();
    await page.locator("#import-text").fill(DERIVED[0]);
    await page.locator("#preview-button").click();
    await expect(page.locator("#preview-known li")).toHaveText([`${DERIVED[0]} — already in Descriptor wallet`]);
    expect(await violations()).toEqual([]);
  } finally {
    await stop(app);
  }
});
