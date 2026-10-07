---
status: proposed
date: 2026-10-06
deciders: repository owner (human), drafted by Claude Code
---

# 0033: A pinned headless Chrome for the E2E tests

## Context and Problem Statement

ENGINEERING §3.1 makes E2E the primary test layer: Playwright drives a browser against the production build, under the real CSP. ENGINEERING §2.3 already says the browser is pinned and hash-checked, but not which one. ENGINEERING §4.1 requires an ADR for "a new dependency with network, native-code or install-time execution capability". A browser is all three of the first two: it's a large native binary, and it can fetch from the network.

`@playwright/test` 1.63.0 (approved in PR #136) expects **Chrome for Testing's headless shell 153.0.8010.12** (playwright-core `browsers.json`, chromium-headless-shell revision 1243). Left to itself, Playwright downloads that browser through its own downloader. That downloader doesn't check a hash we commit, and it runs outside the repository's install path.

The browser runs only on the development machine and in CI, during `make e2e`. It never ships with the app, and the app never starts it: users open the app in their own default browser (ADR 0016).

## Considered Options

1. **Pin Chrome for Testing's headless shell in `scripts/toolchain.lock`** (the build the locked Playwright expects), install it with `make e2e-tools`, and launch it by path.
2. **Let Playwright download its own browser** (`playwright install`). No committed hash, a fetch outside our install path, and its download step is banned for agents.
3. **Use the system's Chrome or Chromium.** Unpinned: it differs between machines and CI, and updates on its own schedule.
4. **Firefox or WebKit builds from Playwright.** The same supply-chain questions as option 1, and Playwright's own builds of these are patched forks with no second source to cross-check.

## Decision Outcome

**Option 1 (proposed: the owner decides when approving PR #153).**

- **The pin:** the four platforms in `scripts/toolchain.lock`, each with a committed SHA-256. There are no publisher checksums, so it's trust-on-first-use against Playwright's CDN, like `sfw` and actionlint (ADR 0026). At pin time, each archive was byte-identical to Google's own copy at `storage.googleapis.com/chrome-for-testing-public/153.0.8010.12/…`, linux-arm64 included.
- **The tie to Playwright:** the lock entry records the Playwright version, and a test checks it against `pnpm-lock.yaml` and Playwright's own `browsers.json`. Bumping one without the other fails.
- **Installing:** `make e2e-tools` installs it like every other pin. That means only after the human approves the pin on `main`, with the archive checked against its hash before a safe zip extraction (no links, no unsafe paths, size and member caps). It's not part of `make toolchain`.
- **Launching:** the E2E target verifies the installed tree, and Playwright launches it through `executablePath`. Playwright's own downloader never runs (`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`).
- **Host libraries:** on Linux the browser needs the host's NSS, ATK, X11, GBM and ALSA libraries. They're host prerequisites, like the host `python3`; this repository doesn't install them. GitHub's Ubuntu runners have them.

### Consequences

- **Good:**
  - E2E runs in a known, hash-checked browser, the same everywhere.
  - Nothing is fetched outside the repository's verified install path.
- **Bad:**
  - **Unreviewed native code.** The browser is a large binary trusted on first use; its code isn't reviewed beyond the cross-checked hash.
  - **It runs with the privileges of whoever runs `make e2e`:** the developer's account or the CI job.
  - **It can reach the network.** The E2E pages are served from loopback under the app's CSP (`connect-src 'self'`), and the spec loads nothing else. But a compromised browser could still open connections of its own.
  - **Residual risk:** a compromised browser could act on the developer's machine or in CI. This is limited by:
    - no real data or credentials where agents and tests run (R-6, R-9)
    - CI jobs with no secrets and read-only contents
    - the hash pin, cross-checked against a second source
  - **This is a test-time risk only.** The shipped app neither contains nor starts the browser. The owner accepts it by accepting this ADR.
- **Re-pinning:** each Playwright update needs a new pin, cross-checked the same way, and a new approval.
