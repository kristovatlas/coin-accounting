// ADR 0037 / THREAT_MODEL T-104, T-604: the graph's stylesheet is built from constants only. No style
// is a function, only `label` may read element data, and a URL-valued property may only name one of
// the bundle's own /assets/ files, chosen by a constant. So chain data can never make Cytoscape load
// an image. The view passes this stylesheet and no other style.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { DATA_MAPPED, STYLESHEET } from "../../frontend/src/graph/style";

// Cytoscape's properties that name a resource: background-image and its kin, and anything URL-like.
const URL_VALUED = /image|url|src|pattern/i;
const READS_DATA = /data\(|mapData\(|mapLayoutData\(|=>|function/;

test("every style is a constant, and no element data reaches a URL-valued property", () => {
  expect(STYLESHEET.length).toBeGreaterThan(0);
  for (const rule of STYLESHEET) {
    for (const [property, value] of Object.entries(rule.style)) {
      expect(["string", "number"], `${rule.selector} ${property}`).toContain(typeof value);
      const text = String(value);
      if (READS_DATA.test(text)) expect(DATA_MAPPED[property], `${rule.selector} ${property}`).toBe(text);
      if (URL_VALUED.test(property)) expect(text, `${rule.selector} ${property}`).toMatch(/^\/assets\/[A-Za-z0-9._-]+$/);
      expect(text, `${rule.selector} ${property}`).not.toMatch(/url\(|https?:|\/\//i);
    }
  }
});

test("the graph view styles Cytoscape with this stylesheet only", () => {
  const view = readFileSync(resolve(__dirname, "..", "..", "frontend", "src", "views", "Graph.tsx"), "utf8");
  expect(view).toContain("style: STYLESHEET");
  expect(view).not.toMatch(/\.style\(|\.css\(|background-image|\bstyle=\{/);
  // the one `style:` key is the stylesheet's: no element is added with a style of its own
  expect(view.match(/\bstyle\s*:/g)).toEqual(["style:"]);
});
