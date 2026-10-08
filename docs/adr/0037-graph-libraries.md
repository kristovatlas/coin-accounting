---
status: accepted
date: 2026-10-08
deciders: repository owner (human), drafted by Claude Code
---

# 0037: Cytoscape.js and @dagrejs/dagre draw the transaction graph

## Context and Problem Statement

M3 draws the user's coins as a graph of transactions and outputs (PLAN §4), with Cytoscape.js and a DAG layout (PLAN's architecture line, DEPENDENCIES.md). ENGINEERING §4.1 requires an ADR for a new dependency with network capability.

Cytoscape.js has one such capability that we know of: a URL-valued style property such as `background-image` makes the browser load that image. M3 confirms this when it installs the package, by searching the 3.33.1 bundle for image loads and request APIs. The graph shows chain data that third parties write: labels, scripts, OP_RETURN text (AD7, T-104). If any of that reached such a property, an attacker would choose what the page loads.

## Considered Options

1. **`cytoscape` with `@dagrejs/dagre`, positions handed to Cytoscape's `preset` layout** (chosen).
2. `cytoscape` with `cytoscape-dagre`. Rejected in PR #203's review: it depends on `dagre` 0.8.5 and `graphlib` 2.1.8, unmaintained 2019 releases, and ships no type declarations.
3. `cytoscape` with `cytoscape-elk`: a much larger layout engine.
4. Hand-written SVG with React: no layout, hit-testing or zoom.

## Decision Outcome

Option 1, with these controls on Cytoscape's image loading:
- **The CSP confines it.** It is `img-src 'self' data: blob:` and `connect-src 'self'` (T-104), so no image can come from another host. Requests to the app's own origin carry no session: the bearer token is only ever sent by the app's own `fetch` calls, as a header (architecture §4). So such a request can reach only the API's unauthenticated answers.
- **M3 will build the graph's stylesheet from constants only.** It will never map element data (label, script, address, text) to a URL-valued style property. Any image a style names, such as a badge, will be one of the bundle's own `/assets/` files, chosen by a constant. M3's first graph PR will add a test that fails if a style maps data to a URL.
- **M3's E2E test will check** that every image request the graph view makes goes to the bundle's own `/assets/` files, and that it causes no CSP violation. A CSP violation means replacing the library, never loosening `style-src` or `script-src`.

`@dagrejs/dagre` and its one dependency, `@dagrejs/graphlib`, only compute positions; they have no network capability.

### Consequences

- Good: the graph uses a maintained, typed, dependency-free renderer, and a maintained layout library.
- Good: the one known network capability is confined by the CSP, whose test exists (`test_the_csp_is_the_strict_one_t104`), and will be by a stylesheet that never reads data, whose test, with the E2E check, arrives in M3's first graph PR.
- Bad: a style that depends on data, such as an icon per entity kind, must choose among constant `/assets/` images by class, never build a URL from data.
- Bad: if Cytoscape needs a looser CSP in some version, the graph needs a different library.

## References

- PLAN §4; ENGINEERING §2.4, §4.1; THREAT_MODEL T-104, T-604; architecture §4
- PRs #203 and #204; `docs/DEPENDENCIES.md`
