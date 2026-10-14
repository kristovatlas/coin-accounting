// M6's events (PLAN §2, §7): enter a purchase and a spend by hand, choose the spend's lot, and see the
// choice kept: the purchase it names can't be removed while it does. The production build under the real
// CSP. Synthetic data only.
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

import { startApp, stop, watchCsp } from "./app";

test("events are entered, a lot is chosen, and a chosen lot's purchase is kept", async ({ page }) => {
  const violations = await watchCsp(page);
  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");

    await page.locator("#new-wallet-name").fill("Cold wallet");
    await page.locator("#new-wallet").click();
    await page.locator("#events-refresh").click();
    await expect(page.locator("#event-account option")).toHaveText(["Account…", "Cold wallet"]);

    const rows = page.locator("#events-list tbody tr");
    await page.locator("#event-kind").selectOption("buy");
    await page.locator("#event-account").selectOption({ label: "Cold wallet" });
    await page.locator("#event-at").fill("2025-01-02T10:00");
    await page.locator("#event-btc").fill("0.001");
    await page.locator("#event-usd").fill("50.00");
    await page.locator("#event-add").click();
    await expect(rows).toHaveCount(1);
    await expect(rows.first().locator(".btc")).toHaveText("0.00100000");

    await page.locator("#event-kind").selectOption("spend");
    await page.locator("#event-at").fill("2025-03-01T10:00");
    await page.locator("#event-btc").fill("0.0004");
    await page.locator("#event-usd").fill("30.00");
    await page.locator("#event-add").click();
    await expect(rows).toHaveCount(2);

    // An amount that isn't BTC is refused before anything is sent.
    await page.locator("#event-btc").fill("1e-3");
    await page.locator("#event-add").click();
    await expect(page.locator("#events-error")).toHaveText("Enter BTC amounts such as 0.015.");
    await expect(rows).toHaveCount(2);

    const spend = rows.filter({ hasText: "Spend" });
    await spend.locator(".choose").click();
    await page.locator("#choose-lots-text").fill("1 0.0004");
    await page.locator("#choose-lots-save").click();
    await expect(spend.locator(".choices")).toContainText("1 0.00040000 (chosen ");

    // The purchase the choice names is kept until the choice changes.
    await rows.filter({ hasText: "Buy (exchange)" }).locator(".remove").click();
    await expect(page.locator("#events-error")).toHaveText(
      "other events' choices name this event's lots: change them first",
    );
    await expect(rows).toHaveCount(2);
    await spend.locator(".remove").click();
    await expect(rows).toHaveCount(1);
    expect(await violations()).toEqual([]);
  } finally {
    await stop(app);
  }
});
