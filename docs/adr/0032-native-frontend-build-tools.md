---
status: proposed
date: 2026-10-06
deciders: repository owner (human), drafted by Claude Code
---

# 0032: Prebuilt native binaries for the frontend build tools (Rolldown, Lightning CSS)

## Context and Problem Statement

ENGINEERING §4.1 requires an ADR for "a new dependency with network, native-code or install-time execution capability". ADR 0024 allowed native code for four Python development tools only, and ADR 0028 for `pydantic-core` at runtime only. Both say any other native package needs its own ADR.

The frontend stack proposed in PR #136 (M0.3 H3) brings native code through **Vite 8.3.1**:
- **`rolldown` 1.2.11** bundles the frontend. It is written in Rust and ships one prebuilt binary per platform (`@rolldown/binding-*`, 15 platforms in the lockfile).
- **`lightningcss` 1.33.0** minifies the CSS. It is written in Rust and ships one prebuilt binary per platform (`lightningcss-*`, 11 platforms).
- **`fsevents` 2.3.3** is the macOS file-watching binding, used by Vite's dev server on macOS only.

These run on the development machine and in CI at build time. They are not part of the shipped app, but the bundler **writes the shipped bundle**, so DEPENDENCIES.md vets it like runtime code. pnpm installs only the binary for the current platform.

**Install scripts:** `rolldown` and `lightningcss` ship prebuilt binaries and have no install script. **`fsevents` 2.3.3 ships a `binding.gyp`, which gives it an implicit `node-gyp rebuild` install step** (the npm registry flags it as having an install script). It is skipped on Linux (`os: [darwin]`). On macOS, `allowBuilds: {}` with `strictDepBuilds: true` makes pnpm refuse to install until that build is either allowed or explicitly denied. The package also ships a prebuilt `fsevents.node`, so denying the build should still leave it working (to be confirmed on macOS before approval).

Every mainstream bundler has native code now: Vite 7 and earlier used `esbuild` (a Go binary per platform), and Rollup 4 ships Rust bindings. A pure-JavaScript bundler means an older or heavier tool, such as webpack, or Rollup 4's WebAssembly build.

## Considered Options

1. **Allow the prebuilt binaries of `rolldown`, `lightningcss` and `fsevents`, for the frontend build only**, under the existing controls.
2. **A pure-JavaScript or WebAssembly bundler** (Rollup 4 with `@rollup/wasm-node`, or webpack), with no native code but a slower, less common build, and some Vite features lost.
3. **Defer the frontend build tooling,** and serve a hand-written static page until a later decision. That blocks the first E2E test and the UI milestones.

## Decision Outcome

Proposed: **1**. The owner decides between the options when approving PR #136.

- **Allowed:** the prebuilt native binaries of `rolldown` (with its `@rolldown/binding-*` packages), `lightningcss` (with its `lightningcss-*` packages) and `fsevents`, at the versions pinned in `pnpm-lock.yaml`. They are **development and build dependencies only**: they run on the developer machine and in CI, and they are never part of the shipped app.
- **`fsevents`' build step is denied, not allowed** (proposed): add an explicit deny for `fsevents` to `allowBuilds` in `pnpm-workspace.yaml` (the exact syntax for the pinned pnpm to be confirmed), so no install-time compile runs and macOS installs aren't blocked. Allowing the build instead would be install-time code execution, which this ADR does not cover. **The owner decides this together with the rest of the ADR**, and confirms a macOS install before approving.
- **Not allowed by this ADR:** any other native JavaScript package, including TypeScript 7's native compiler (DEPENDENCIES.md pins TypeScript 6.0.3, the last pure-JavaScript release). Each needs its own ADR.
- **Unchanged controls:**
  - `make propose-js` resolves only, and the owner approves the Socket report and the lockfile diff (§2.4)
  - the 7-day cooldown
  - `allowBuilds`: no install script runs (empty except the explicit `fsevents` deny above)
  - `blockExoticSubdeps`
  - the lockfile policy check: no explicit non-registry source, one sha512 integrity per package (the configured registry is pinned by the `.npmrc`/environment checks, #93 and #36)
- **Residual risk:** the compiled code (Rust in `rolldown` and `lightningcss`; C++ in `fsevents`) isn't reviewed beyond its hashes and Socket's report. A compromised bundler could change the **shipped bundle**, which runs in the user's browser with the session token. That is a frontend supply-chain risk (THREAT_MODEL T-601, T-604, and the browser-side threats T-104 and T-106), not the backend in-process risk R-4 covers. It is limited by:
  - the production-build E2E tests under the real CSP (ENGINEERING §3.1), which fail if the bundle reaches an external host (T-106)
  - the reproducible-build comparison of the frontend bundle (ENGINEERING §2.7)

  The bundle's content isn't otherwise reviewed. **The owner accepts this residual risk by accepting this ADR,** and it is recorded as a new accepted risk in THREAT_MODEL in the same PR that accepts the ADR.

### Consequences

- Good: the standard, fast Vite build, and the E2E tests on the production build.
- Bad: compiled code from three projects (Rolldown, Lightning CSS, fsevents) runs at build time and writes the shipped bundle. Socket and human review see less of it than of JavaScript.
- Neutral: `fsevents` matters only on macOS. pnpm installs one platform binary per package, so the other platforms' entries are only in the lockfile.

## References

- ADR 0002 (local web app), ADR 0013 (supply-chain policy), ADR 0024 (native-code dev tools), ADR 0028 (pydantic-core at runtime)
- ENGINEERING §2.1, §2.4, §3.1, §4.1; THREAT_MODEL T-601, T-602, T-604; R-4
- PR #136 (M0.3 H3), docs/DEPENDENCIES.md
