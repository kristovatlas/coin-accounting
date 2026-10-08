// The graph's stylesheet (PLAN §4; ADR 0037, THREAT_MODEL T-104, T-604). Built from constants only:
// no style is a function, and no element data is ever mapped to a URL-valued property, so chain data
// can't make Cytoscape load an image. The one data mapping is `label`, which Cytoscape draws as text.
// Colours tell owners apart by class (`mine`, `tagged`, `untagged`, `unspendable`), set from the API's
// owner field, never from free text. `e2e/tests/graph-style.spec.ts` checks all of this.

export type StyleRule = { selector: string; style: Record<string, string | number> };

/** The only style properties that may read element data, and the only data they may read. */
export const DATA_MAPPED: Readonly<Record<string, string>> = { label: "data(label)" };

export const STYLESHEET: readonly StyleRule[] = [
  {
    selector: "node",
    style: {
      label: "data(label)",
      "font-size": 10,
      "text-wrap": "wrap",
      "text-max-width": "160px",
      "text-valign": "bottom",
      "text-margin-y": 4,
      "border-width": 1,
      "border-color": "#555555",
    },
  },
  { selector: "node.tx", style: { shape: "round-rectangle", width: 28, height: 28, "background-color": "#d9d9d9" } },
  { selector: "node.tx.mixing", style: { "border-width": 3, "border-style": "double", "border-color": "#7a3fb0" } },
  { selector: "node.output", style: { shape: "ellipse", width: 18, height: 18 } },
  { selector: "node.mine", style: { "background-color": "#2e7d32" } },
  { selector: "node.tagged", style: { "background-color": "#1565c0" } },
  { selector: "node.untagged", style: { "background-color": "#ffffff" } },
  { selector: "node.unspendable", style: { "background-color": "#9e9e9e", shape: "diamond" } },
  { selector: "node:selected", style: { "border-width": 3, "border-color": "#ff8f00" } },
  {
    selector: "edge",
    style: {
      width: 1.5,
      "line-color": "#888888",
      "target-arrow-color": "#888888",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
    },
  },
];
