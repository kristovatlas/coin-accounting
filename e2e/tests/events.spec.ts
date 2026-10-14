// M6's events (PLAN §2, §7): enter a purchase, a gift and a spend by hand, see refusals before
// anything is sent, and see that a wallet in automatic mode (ADR 0021 §1) offers no lot picker.
// The production build under the real CSP. Synthetic data only.
import { pathToFileURL } from "node:url";

import { expect, test } from "@playwright/test";

import { startApp, stop, watchCsp } from "./app";

test("events are entered, refused before sending when malformed, and removed", async ({ page }) => {
  const violations = await watchCsp(page);
  const app = await startApp();
  try {
    await page.goto(pathToFileURL(app.launchFile).href);
    await expect(page.locator("#status")).toHaveText("Connected to Bitcoin Core (regtest).");

    await page.locator("#new-wallet-name").fill("Cold wallet");
    await page.locator("#new-wallet").click();
    await expect(page.locator("#import-account option")).toHaveText(["Cold wallet"]); // created
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

    // A gift received keeps the donor's basis, the FMV and the donor's date.
    await page.locator("#event-kind").selectOption("gift_in");
    await page.locator("#event-at").fill("2025-02-01T10:00");
    await page.locator("#event-btc").fill("0.0002");
    await page.locator("#event-usd").fill("12.00");
    await page.locator("#event-fmv").fill("20.00");
    await page.locator("#event-donor-date").fill("2020-05-01");
    await page.locator("#event-add").click();
    await expect(rows).toHaveCount(3);
    await expect(rows.filter({ hasText: "Gift received" }).locator(".usd")).toHaveText("12.00");

    // USD text the API can't take is refused before anything is sent.
    await page.locator("#event-kind").selectOption("spend");
    await page.locator("#event-btc").fill("0.0001");
    await page.locator("#event-usd").fill("$1,000");
    await page.locator("#event-add").click();
    await expect(page.locator("#events-error")).toHaveText("Enter USD amounts such as 1250.50, without $ or commas.");
    await page.locator("#event-usd").fill("30.00");

    // An amount that isn't BTC is refused before anything is sent.
    await page.locator("#event-btc").fill("1e-3");
    await page.locator("#event-add").click();
    await expect(page.locator("#events-error")).toHaveText("Enter BTC amounts such as 0.015.");
    await expect(rows).toHaveCount(3);

    // A wallet selects its lots by its standing method by default (ADR 0021 §1): no lot picker.
    const spend = rows.filter({ hasText: "Spend" });
    await expect(spend.locator(".choose")).toHaveCount(0);

    await spend.locator(".remove").click();
    await expect(rows).toHaveCount(2);
    expect(await violations()).toEqual([]);
  } finally {
    await stop(app);
  }
});
