# Engineering Practices — Coin Accounting

> **Binding once approved.** From the first line of code, these practices apply to every contributor, human or AI agent. Changing them requires a PR that edits this file and gets human approval. A change that weakens a control also needs an ADR and an update to [`THREAT_MODEL.md`](THREAT_MODEL.md).

| | |
|---|---|
| Version | 0.2.5 |
| Last updated | 2026-09-27 |
| Related | [`PLAN.md`](../PLAN.md) · [`THREAT_MODEL.md`](THREAT_MODEL.md) · [`DEPENDENCIES.md`](DEPENDENCIES.md) · `docs/adr/` · `docs/architecture.md` |

Items marked **(verify at setup)** depend on tool behaviour to be confirmed when M0 configures the toolchain. If a tool doesn't behave as described, the M0 PR must propose an equivalent control here. Tool versions referenced: pnpm 12.x, uv (current), Socket Firewall Free 1.15.x, as of 2026-09.

---

## 1. Principles

1. **Privacy and correctness before features.** The app handles data that can deanonymize the user and figures that go on tax returns. When in doubt, choose the conservative option and write an ADR.
2. **Few dependencies.** Every dependency is attack surface. In-process egress is an accepted risk (THREAT_MODEL R-4), so **supply-chain controls are the primary defence against data exfiltration**. Prefer the standard library, or a small, well-understood module we write ourselves.
3. **Nothing third-party executes before it is vetted.** A package is resolved, reviewed and approved *before* it is installed (§2.4).
4. **Evidence over assertion.** A feature is done when an end-to-end test shows it working, not when unit tests pass.
5. **Decisions are written down.** Significant choices go in ADRs; the architecture diagram and threat model stay in sync with the code.

## 2. Supply chain

Applies to **runtime and development dependencies alike**: linters, test tools and build tools run code on the developer's machine too. Frontend build tools (Vite and its plugins, TypeScript) write the shipped bundle, so they are vetted like runtime dependencies.

### 2.1 JavaScript (frontend)

Configured in `pnpm-workspace.yaml`. Since pnpm 11, `.npmrc` is read only for auth and registry settings.

| Control | Setting |
|---|---|
| Package manager | **pnpm ≥ 12**, version pinned via `packageManager` in `package.json`. `pmOnFail: error`, so pnpm never downloads a different version of itself. npm/yarn are not used |
| Cooldown | `minimumReleaseAge: 10080` (**7 days**), `minimumReleaseAgeStrict: true`, `minimumReleaseAgeIgnoreMissingTime: false`. Exceptions (§2.6) are **version-specific** (`pkg@x.y.z` in `minimumReleaseAgeExclude`) and carry an expiry |
| Dependency build scripts | `allowBuilds: {}` (empty) with `strictDepBuilds: true`: any dependency with a build script fails the install. Allowing one requires an ADR |
| Our own scripts | Our `package.json` files contain **no lifecycle scripts** (`preinstall`/`install`/`postinstall`/`prepare`); there are no `.pnpmfile.*` hooks and no `configDependencies`. Enforced by the CI check in §2.5 |
| Exotic sources | `blockExoticSubdeps: true` blocks git, tarball and URL dependencies anywhere in the tree; `trustPolicy: no-downgrade` |
| Auto-install | `verifyDepsBeforeRun: error`: `pnpm run`/`exec` refuse to run with stale dependencies instead of installing silently |
| Lockfile | `pnpm-lock.yaml` committed; installs use `--frozen-lockfile` |
| Runtime assets | Everything bundled; no CDN, web fonts or remote resources (THREAT_MODEL T-106) |
| Node.js | Version pinned (`.node-version`), installed from nodejs.org release tarballs checked against the signed `SHASUMS256.txt` (§2.3) |

### 2.2 Python (backend)

Configured in `pyproject.toml` `[tool.uv]`.

| Control | Setting |
|---|---|
| Package manager | **uv**, version pinned; installed via §2.3 |
| Python | Version pinned in `.python-version` (**3.13**). `python-downloads = "never"`: uv never fetches an interpreter by itself. Python is installed by the verified bootstrap (§2.3) |
| Cooldown | `exclude-newer = "7 days"` (relative, recorded in `uv.lock`). Exceptions are per package via `exclude-newer-package`, with an expiry (§2.6) |
| No sdist builds | `no-build = true`: only wheels are installed, and there are **no exceptions**. A needed package that ships only an sdist is a reason to find an alternative, or needs an ADR that temporarily changes this setting for one exact pinned version |
| Our own project | `package = false` (virtual project): uv never downloads or runs a build backend for our own code |
| Auto-sync | `UV_NO_SYNC=1` is exported by the `Makefile` and required in `AGENTS.md`, so `uv run` never locks or syncs by itself |
| Sources | PyPI only. No extra indexes, no `[tool.uv.sources]` git, URL or path entries, no PEP 508 direct URLs (CI check, §2.5) |
| Lockfile | `uv.lock` (with hashes) committed; installs use `--locked` |
| `.pth` files | Wheels can ship `.pth` files that run on every interpreter start. CI lists `.pth` files in the environment and fails on any not on an allowlist |

### 2.3 All installs go through Socket Firewall, via the repo's scripts

**Every command that downloads or installs a package, for any purpose (runtime, dev tooling, tests, CI), runs through Socket Firewall (`sfw`)**, and only through the repo's own entry points. Nobody types a bare install command.

- **Single entry point:** a top-level `Makefile` holds every install and update action. It calls small scripts in `scripts/` where logic is needed. It is written for macOS's GNU Make 3.81 and bash 3.2 as well as Linux, e.g. `shasum -a 256`, not `sha256sum`.

  | Target | Does |
  |---|---|
  | `make toolchain` | Installs the pinned `sfw` (see below), then the pinned pnpm, uv, Node and Python, each verified by checksum |
  | `make bootstrap` | `sfw pnpm install --frozen-lockfile` and `sfw uv sync --locked`, from a clean cache in CI |
  | `make propose-js PKG=<name@version> WORKSPACE=frontend\|e2e [DEV=1]` / `make propose-py …` | **Resolve only**: `sfw pnpm add --lockfile-only …` / `sfw uv add --no-sync …`. Nothing is installed. Prints the lockfile diff and the list of new packages to vet (§2.4) |
  | `make update-deps` | Batch update (§2.6), lockfile-only, then the same review |
  | `make update-sfw` | Reviewed update of the pinned `sfw` version and checksum |
  | `make test-tools` | Pinned, verified test tooling downloads (see "Non-package downloads") |
  | `make audit` | `pip-audit` and `pnpm audit` against the lockfiles (tools locked as dev dependencies; network through `sfw`) |

  Contributors, AI agents and CI all use these targets. CI jobs call `make toolchain` and `make bootstrap`, never raw installers.
- **No silent fallback:** if `sfw` is missing, not the pinned version, or fails to start, the scripts **stop with an error**. They never drop through to an unwrapped install. Whether `sfw` fails open when the Socket API is unreachable must be tested at setup. If it does, the wrapper detects that and stops **(verify at setup)**.
- **Fetch-and-run commands are banned:** `npx`, `pnpm dlx`, `pnpm exec` of packages not in the lockfile, `uvx`/`uv tool run`, `pip install`, `curl … | sh`, the `pre-commit` framework (it clones and builds hook environments). The same applies to IDE and agent configuration, e.g. `.mcp.json` servers started with `npx -y`. Tools we need become locked dev dependencies. Git hooks, if any, are `repo`-local scripts that call locked tools.
- **Verifying before use:** every install target first runs `toolchain.py verify sfw pnpm uv`, with a host interpreter the caller can't override, and then calls the tools by absolute path. A missing or modified binary stops the command; there's no fallback to a host `pnpm`/`uv`.
- **Host requirement:** `python3` 3.9 or newer to run the repository scripts; the lock is JSON so no `tomllib` is needed.
- **Bootstrapping `sfw` itself:** `make toolchain` (`scripts/toolchain.py`) downloads the pinned sfw-free release binary and checks it against the SHA-256 in `scripts/toolchain.lock`. **Socket publishes no checksums or signatures for sfw-free**, so that committed hash is trust-on-first-use. It is recorded when the version is first adopted, and changes only through `make update-sfw` in a reviewed PR. macOS binaries are unsigned. CI uses the same script, not `socketdev/action`, which installs the latest version.
- **What `sfw` does and doesn't do:**
  - It blocks packages Socket has *confirmed* as malware.
  - AI-flagged risks produce warnings only, and brand-new unscanned versions are not blocked. That is why the §2.4 review and the cooldowns still matter.
  - It sends the names and versions of the packages being installed to Socket. This is a build-time flow listed in THREAT_MODEL §6.
- **Non-package downloads** that `sfw` cannot inspect each have an explicit verification method, recorded in `DEPENDENCIES.md` with version and hash:

  | Artifact | Verification |
  |---|---|
  | `sfw` | Committed SHA-256 (trust-on-first-use, as above) |
  | pnpm | npm registry tarball checked against the registry's sha512 integrity, committed in the lock (registry signature and provenance: M0.2) |
  | uv | Release artifacts checked against the publisher's checksums, and our committed SHA-256 |
  | Node.js | `SHASUMS256.txt` verified against the Node release keys |
  | Python | Pinned interpreter build checked against a committed SHA-256 |
  | `bitcoind` (regtest) | `SHA256SUMS` plus a threshold of builder signatures (pinned `guix.sigs` builder keys), minimum and latest supported versions |
  | Playwright browsers | Pinned `@playwright/test` version; each downloaded browser archive checked against a committed per-platform SHA-256; fails closed if no hash is recorded |

- **Enforcement:** a CI check (`scripts/check-install-commands`) scans the `Makefile`, `scripts/`, `.github/workflows/` and config files for install or fetch-and-run commands without the `sfw` wrapper. It can be bypassed by obfuscation, so it is **hygiene, not a security boundary** (ADR 0022: the Claude Code guard and this check catch accidental or habitual installs; deliberate evasion is accepted risk R-8), and it allowlists documentation files that quote the banned commands. `AGENTS.md` repeats the rule for agents.

### 2.4 Adding a dependency: vet before anything is installed

The same process applies to a new direct dependency, and to a version bump of an existing one:

1. **Justify it.** Why can't the standard library or ~100 lines of our own code do the job? Could a package we already use do it?
2. **Resolve only.** Run `make propose-js`/`make propose-py`. This updates the manifest and lockfile without installing or running anything.
3. **Review the Socket verdict** for every new or changed package in the lockfile diff. Use the Socket GitHub App's report on the draft PR (which contains only the manifest/lockfile change), or the package's socket.dev page. Look for install scripts, network or filesystem access, obfuscated code, telemetry, new maintainers and typosquat signals.
4. **Check its health:** maintainers, release history, open advisories, download base, licence, transitive dependency count.
5. **Record it** in [`DEPENDENCIES.md`](DEPENDENCIES.md).
6. **Human approval.** The human explicitly approves the dependency in the PR. AI agents may propose dependencies, never approve them.
7. **Only then install**, with `make bootstrap`.

**Transitive dependencies** don't each need steps 1, 4 and 5. They are covered by:
- the Socket diff on the PR
- the lockfile policy check (§2.5)
- a `DEPENDENCIES.md` entry only for packages Socket flags, or that have network capability, native code or install-time execution

### 2.5 Lockfile policy check (CI, required)

The cooldown only applies when versions are *resolved*. A hand-edited or bot-generated lockfile could still bring in a fresh or off-registry package. So a required CI check (`scripts/check-lockfiles`, added in M0.2 together with the first lockfile) verifies, for **every** entry in `pnpm-lock.yaml` and `uv.lock`:

- the source is `registry.npmjs.org` or `files.pythonhosted.org`, with no git, URL, tarball or path sources, direct or transitive
- an integrity hash is present
- the version was published **≥ 7 days** ago (npm registry `time`; `upload-time` in `uv.lock`), unless it has a recorded, unexpired exception (§2.6)
- our `package.json` files have no lifecycle scripts, and there is no `.pnpmfile.*` and no `configDependencies`
- the pnpm lockfile is parsed in full. pnpm 12 can write multi-document lockfiles, and scanners that read only the first document report zero dependencies **(verify at setup that the Socket App and Dependabot handle this)**

### 2.6 Updating dependencies

- Updates come in **deliberate batches** (at most monthly, plus urgent security fixes), never as a side effect of other work.
- Dependabot runs on a monthly schedule with grouped updates and a 7-day cooldown. It covers the `npm`, `uv` and `github-actions` ecosystems, the last so that SHA-pinned actions get updates. Dependabot changes **manifests as well as lockfiles**. Its PRs go through the §2.4 review in full.
- **Dependabot security updates ignore the cooldown**, and its cooldown covers only version updates of direct dependencies. The lockfile policy check (§2.5) is what enforces the 7-day rule everywhere.
- **Security-fix exceptions to the cooldown:**
  - Only for a vulnerability that is exploitable in this app. It runs locally on loopback, so many CVEs aren't.
  - The PR assesses exploitability against the threat model.
  - The exception is version-specific, has an expiry of at most 7 days, and is recorded in `DEPENDENCIES.md`.
- A weekly scheduled CI job runs `make bootstrap` from an empty cache, so `sfw` rechecks every locked package against current Socket data, and runs `make audit`.

### 2.7 CI and repository

- **GitHub Actions:**
  - pinned to full commit SHAs with a version comment
  - only `actions/*` or vetted publishers
  - `zizmor` and `actionlint` run on workflow files (they catch impostor-commit pins and unsafe patterns)
  - `permissions: contents: read` by default, and the repository default token is read-only
  - `persist-credentials: false` on checkout
  - no `pull_request_target`
  - job timeouts set
  - "Allow GitHub Actions to create and approve pull requests" is disabled
  - PRs from outside contributors' forks need approval before workflows run, since the repository is public
- **Secrets:** CI needs none. The Socket GitHub App is used rather than the Socket CLI, which would need an API token.
- **Caches:** no dependency caches in CI (`sfw` can't inspect cached artifacts). On developer machines, pnpm `storeDir` and uv `cache-dir` are project-local, so packages vetted for other projects are never reused unchecked.
- **Checks on every PR:**
  - `make audit` (`pip-audit` + `pnpm audit`)
  - the lockfile policy check
  - the install-command check
  - the Socket App report
  - tests and coverage on **Linux and macOS**
  - a **reproducible frontend build**: build twice in the same pinned environment and compare the normalized `dist/` output
- **Branch protection on `main`:**
  - PRs only, with all required checks green
  - stacked PRs are merged with merge commits
  - **only the human merges**, or explicitly tells an agent to merge. Agents act with the owner's GitHub credentials, so this is a **procedural** rule, not a technical one: user decision, 2026-09-27; THREAT_MODEL T-605
- **Commit signing is not required** (user decision, 2026-09-27). Agents would need the owner's key, so signatures couldn't tell agent commits from human ones. GitHub signs the merge commits it creates. If releases are ever published for other users, release tags will be signed (THREAT_MODEL T-606).
- GitHub secret scanning and push protection are enabled.

## 3. Testing

### 3.1 Strategy: end-to-end first

| Layer | What it proves | Tooling |
|---|---|---|
| **E2E** (primary) | A user flow works: real regtest `bitcoind` + backend + production frontend build, served with the real CSP | **`@playwright/test`** specs in `e2e/`, with a Python harness in `e2e/harness/` that starts regtest `bitcoind` and the backend, and reads the bootstrap file (architecture §4) |
| Integration | A component works against real neighbours (RPC client and scan jobs ↔ regtest node, cache ↔ DB, API ↔ DB) | `pytest` + regtest |
| Unit | Pure logic is correct at the edges (scan-result processing, doxx rules, tax engine, box selection) | `pytest`, Hypothesis for property tests and fuzzing |
| Frontend unit | Components with non-trivial logic | `vitest` |

- **Every user-visible feature ships with at least one E2E test** covering its main path. This is part of the Definition of Done (§8).
- **E2E runs against the production build under the real CSP.** Any `securitypolicyviolation` event or CSP console error fails the test. This is how inline styles injected by Cytoscape or libraries get caught.
- **Regtest `bitcoind`** runs at both the minimum supported and the latest Core version.
- **Mocks only at process boundaries we can't run.** The Bitcoin node is *not* mocked; tests use regtest. Mocking our own modules is not allowed in `tax/` and `doxx/` tests.
- Tax scenarios use **hand-worked expected values derived from IRS rules**. Each test cites the rule, e.g. `# Treas. Reg. §1.1012-1(j)`, `# 2025 i8949 box I`.
- **Fixtures use public chain data or synthetic data only.** Never use the developer's own transactions or addresses as fixtures, because choosing them reveals ownership.
- **pytest plugins** are loaded explicitly: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` plus `-p` for each allowed plugin, so no dev dependency can inject one.

### 3.2 Socket guard (tests)

A pytest fixture, active for the whole suite, patches `socket.socket.connect` and `socket.getaddrinfo`. It fails any test that opens a connection or does a DNS lookup other than:
- loopback to the regtest node
- loopback to the app under test

Price-fetch tests use a local stub server. The guard catches accidental phoning home (THREAT_MODEL T-305, T-604). It is **not a security boundary** against malicious code.

### 3.3 Coverage floors (CI-enforced)

| Scope | Floor | Measured from |
|---|---|---|
| Backend overall | **≥ 85 %** line + branch | Unit + integration + E2E combined |
| `tax/`, `doxx/`, `chain/` | **≥ 95 %** line + branch | **Unit + integration only**, so incidental execution during E2E doesn't count |
| Frontend | **≥ 70 %** lines | `vitest` unit coverage; E2E flows are required separately (§3.1) |

- **E2E backend coverage:**
  - coverage.py ≥ 7.10 with `[run] parallel = true`, `patch = ["subprocess"]` and `sigterm = true`
  - the server is shut down gracefully so its data is written
  - `coverage combine` merges the results
- Floors are minimums, not targets.
- A merge-base comparison fails a PR that lowers coverage in a floored module by more than 0.5 percentage points. The ratchet uses the deterministic unit + integration numbers only.

### 3.4 Mutation testing

`mutmut` runs on `tax/` and `doxx/`, using unit and integration tests only:
- **on every PR that changes those files**, limited to the changed files
- weekly in full, and before each milestone is closed

**Budget: zero unexplained survivors** in `tax/engine.py` and the box-selection rules, and ≤ 5 % elsewhere in those modules. An equivalent mutant is excluded only when it is listed with a reason in `tests/mutation-exclusions.md`.

### 3.5 Anti-test-slop rules

A test exists to fail when behaviour breaks. Reviewers (human and AI) reject tests that:

1. **Assert nothing meaningful:** `assert result`, `assert x is not None`, or `assert True` alone; asserting only that a function was called.
2. **Mirror the implementation:** expected values computed by the same logic under test.
3. **Mock the thing under test,** or mock our own modules in `tax/`/`doxx/` tests. *Automated:* ruff `TID251` bans `unittest.mock` in `backend/tests/{unit,integration}/{tax,doxx,domain}/` (tests are organised per module, architecture §2).
4. **Snapshot everything:** whole-report snapshots are allowed only as hand-reviewed golden files, with a README explaining how each value was derived.
5. **Were changed to match new output** without an explanation in the PR of why the old expectation was wrong.
6. **Depend on timing or order:** `sleep`-based waits, wall-clock dates, test order, or network access (the socket guard, §3.2, fails these). *Automated:* `time.sleep` is banned in tests.
7. **Duplicate another test** without adding a distinct case.
8. **Have unclear names.** Names must state the behaviour and, where relevant, the threat or rule ID in the form `t508`, e.g. `test_late_identification_is_flagged_not_overridden_t508`.

**Test audits:** at each milestone close, and at least monthly while coding is active, a test-audit pass reviews the suite against these rules plus the mutation report. It deletes or strengthens weak tests, and its findings go into the milestone PR. An AI reviewer may do a first pass; a human approves the result.

## 4. Design records

### 4.1 ADRs

- **Location/format:**
  - `docs/adr/NNNN-kebab-title.md`, [MADR](https://adr.github.io/madr/) template
  - Status: `proposed` → `accepted` or `rejected`, later `deprecated` or `superseded by NNNN`
  - **Acceptance means the human merged the PR** that contains the ADR
- **After acceptance, only the Status line may change.** A CI check rejects any other edit to an accepted ADR. To change a decision, write a new ADR that supersedes the old one.
- **An ADR is required for:**
  - a new runtime network flow
  - a new data store, or moving data across the VeraCrypt boundary
  - a new dependency with network, native-code or install-time execution capability
  - changes to tax rules or doxx rules
  - storage format changes, or a change in how chain data is obtained from the node
  - weakening any control in this document or the threat model
  - changes to the architecture diagram
  - supported platform changes

### 4.2 Architecture diagram

- `docs/architecture.md` holds the Mermaid component diagram and the data-flow diagram, reviewed by the human.
- The accepted ADR that last changed the diagram records the file's SHA-256 in its front matter. A CI check fails if the current file doesn't match that hash. The check proves the diagram and ADR are **consistent**. **Approval** is the human's merge of that PR.
- Code that contradicts the diagram is a bug. Either fix the code, or change the diagram through an ADR.

### 4.3 Threat model

- Any PR touching a boundary, asset, store, network flow (including build-time flows), dependency or tax rule updates `THREAT_MODEL.md` in the same PR: statuses, evidence links, changelog.
- A threat moves to **Verified** only when a linked test would fail if the mitigation were removed.

## 5. Code standards

### 5.1 Languages and tooling

| Area | Standard |
|---|---|
| Python | 3.13; `ruff` (lint + format); `mypy --strict`; no `# type: ignore` without a reason comment |
| TypeScript | `strict: true`; ESLint (incl. React rules); no `any` without a reason comment |
| SQL | Parameterized queries only; schema changes via migrations |
| Formatting | Enforced in CI; not debated in review |

### 5.2 Rules enforced by lint or custom checks

| Rule | Why | Enforcement |
|---|---|---|
| Import edges and capabilities (network, `webbrowser`, `subprocess`, filesystem, clock, `importlib`/`__import__`) follow the per-path rules in [architecture §2](architecture.md#2-module-structure-dependency-rules-and-capability-rules) | T-305, T-402, purity of `tax/`/`doxx/`/`domain/` | `scripts/check-architecture` (custom AST check, no dependency) + ruff `banned-api` per path. Hygiene, not a security boundary |
| No floats in `tax/`: no float literals, **no true division `/` or `/=` at all** (`//` for exact integer division; Decimal division only through a named helper in `domain/`), no `float()`, no `math`; money arrives as strings or `Decimal`; the `decimal` context in `tax/` traps `FloatOperation`; property tests check that no intermediate value is a float | T-502 | custom AST check + runtime trap + tests |
| No `eval`/`exec`, `pickle`, `shell=True` | Code execution | ruff (`S` rules) + banned-api |
| No `dangerouslySetInnerHTML`; no runtime CSS-in-JS (it injects `<style>` tags that the CSP blocks); no `setAttribute('style', …)` | T-104 | ESLint rules + E2E CSP-violation check (§3.1) |
| No navigation or `window.open` to external origins; no network sinks (`fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `sendBeacon`) outside the API client module | T-106 | ESLint rules + a bundle scan for network sinks. Plain URL strings such as React's error-doc links are allowlisted, since they are inert |
| Timestamps are timezone-aware UTC; conversion to local time only for display and for tax dates | Tax dates (PLAN §7) | lint + review |
| Logging only through the redacting logger; never `print` user data | T-403 | ruff (`T20`) + review |
| No secrets, real addresses, xpubs or txids of the developer, or real user data in code, fixtures, issues or PRs | T-607 | secret scanning + a CI regex check for mainnet addresses/xpubs, with an allowlist of well-known public ones + review |

React's `style` prop sets styles through the CSSOM, which `style-src` does not block. It is discouraged for maintainability, not banned for security.

### 5.3 Error handling

- **Fail closed** on anything touching integrity: chain-data fetching and caching, reorgs, storage checks, price imports.
- Never swallow exceptions silently. User-facing errors must not echo sensitive values.

## 6. AI coding agents

Agents (Claude Code, Codex and others) follow `AGENTS.md`, which makes this document and the threat model binding. Their instructions live in `AGENTS.md`; `CLAUDE.md` only imports it. In addition:

- **No real data:**
  - Agents never run while a VeraCrypt volume with real data is mounted, and never get access to real DBs, logs, exports or configs. Development and E2E use regtest and synthetic data only. Mainnet smoke tests are run by the human (THREAT_MODEL T-607).
  - A Claude Code `SessionStart` hook in the repo refuses to start a session if a VeraCrypt mapping is present **(verify at setup)**.
  - More broadly, the user documentation warns against doing development work, or running AI coding agents, on a machine where real financial data is used (THREAT_MODEL R-6).
- **Scope.** Agents don't:
  - add or approve dependencies outside the §2.4 process
  - run any install or fetch-and-run command outside the `make` targets (§2.3); `UV_NO_SYNC=1` is always set
  - weaken a control
  - change `docs/architecture.md` without an ADR
  - commit to `main` directly or merge PRs unless the human explicitly says so (§2.7)
  - add MCP servers or tools that fetch and run packages
- **Transparency.** Agent-authored commits carry a `Co-Authored-By` trailer. PR descriptions state what was verified (commands run, tests added) and what wasn't.
- **Review.** Every agent PR gets human review. Independent AI reviews (e.g. a second model) are encouraged for design docs and security-relevant code. Their findings are verified before being acted on, not applied blindly. Review output and the triage decisions are posted as PR comments, as a record.

## 7. Workflow

- **Branches:** `main` is always releasable. Work happens on short-lived branches (`<type>/<topic>`, e.g. `feat/scan-jobs`, `docs/adr-0003`).
- **PRs:**
  - small and focused
  - opened as **drafts** for human review on GitHub
  - stacked PRs are allowed and merged with merge commits
  - each PR description lists the affected threat IDs and ADRs
- **Commits:** imperative subject ≤ 72 chars; the body explains *why*.
- **Docs travel with code:** PLAN, threat model, ADRs, diagram and `DEPENDENCIES.md` are updated in the same PR as the change that affects them.

## 8. Definition of Done

A change is done only when:

- [ ] Behaviour is covered by an **E2E test** (for user-visible features) and by unit/integration tests where logic warrants them
- [ ] Coverage floors hold; the PR mutation run holds for changed `tax/`/`doxx/` files; CI is green on Linux and macOS
- [ ] Tests comply with §3.5, with no slop
- [ ] `THREAT_MODEL.md` statuses and evidence links are updated (or explicitly "no change")
- [ ] ADR written or updated if §4.1 applies; the architecture diagram still matches
- [ ] Any new dependency went through §2.4, is recorded in `DEPENDENCIES.md`, and has human approval
- [ ] No new network flow outside THREAT_MODEL §6; the socket guard stays green
- [ ] User-facing docs updated where behaviour changed
- [ ] The human merged the PR

## 9. Enforcement summary

| Practice | Enforced by |
|---|---|
| Cooldowns, exotic-source bans, no build scripts, wheels-only, no auto-install/auto-download | Tool config (§2.1, §2.2) + lockfile policy check (§2.5) |
| Vet before install | `make propose-*` (lockfile-only) + Socket App report + human approval |
| Socket Firewall on all installs | `make` targets wrapping `sfw` with no fallback + install-command check (hygiene) |
| Vulnerability audits | `make audit` on every PR + weekly clean-cache job |
| Non-package downloads | Per-artifact verification (§2.3) |
| Actions hardening | `zizmor` + `actionlint` + repository settings |
| Coverage floors and ratchet | CI (`coverage.py`, `vitest`, merge-base comparison) |
| Mutation budget | Per-PR targeted run + weekly full run |
| Banned APIs, float ban, CSP/XSS rules | ruff / AST check / `FloatOperation` trap / ESLint / bundle scan / E2E CSP check |
| Module import edges and capability rules | `scripts/check-architecture` (architecture §2) |
| ADR immutability, diagram hash | CI checks |
| Threat model / ADR / DEPENDENCIES updates | PR template checklist + human review |
| Test-slop rules | Partly automated (§3.5) + review checklist + periodic test audit |
| Only the human merges | Procedural (Documented, T-605) |
| No real data for agents | `AGENTS.md` + SessionStart hook + human discipline (Documented, T-607, R-6) |

## 10. Changelog

| Date | Version | Change |
|---|---|---|
| 2026-09-27 | 0.1 | Initial proposal (P0.2), including the rule that all installs go through `sfw` via the repo's `make` targets (§2.3) |
| 2026-09-27 | 0.2 | Incorporates the Opus 5.5 and Codex (gpt-5.6-sol) reviews and the user's decisions: two-phase dependency adds (resolve → vet → approve → install); pnpm 12 settings (`allowBuilds`, `pmOnFail`, `verifyDepsBeforeRun`, `blockExoticSubdeps`, `trustPolicy`); uv `package = false`, `python-downloads = "never"`, `UV_NO_SYNC`, no sdist exceptions, relative `exclude-newer`; lockfile policy check; sfw trust-on-first-use + telemetry; verification table for non-package downloads; audits, reproducible build, Actions hardening restored or added; socket guard defined; coverage mechanics and ratchet; per-PR mutation runs; corrected CSP rationale; float ban hardened; ADR immutability check. **Signed commits dropped** and **only-the-human-merges kept procedural** by user decision |
| 2026-09-27 | 0.2.1 | Aligned with architecture v0.2: `doxx/` is a pure package; E2E lives in `e2e/` with a Python harness; import and capability rules are defined in architecture §2 and checked by `scripts/check-architecture`; the subprocess allowlist moves there |
| 2026-09-28 | 0.2.2 | M0.1: the toolchain installer is `scripts/toolchain.py` with `scripts/toolchain.lock`; `check-lockfiles` arrives with the first lockfile (M0.2) |
| 2026-09-28 | 0.2.3 | `/` is banned outright in `tax/` (#10); enforced by `scripts/check_architecture.py` |
| 2026-09-28 | 0.2.4 | PR #7 review round 2: verify sfw, pnpm and uv before every install; non-overridable verifier interpreter; `WORKSPACE` for `propose-js`; host Python 3.9+; Linux ARM64 |
| 2026-09-28 | 0.2.5 | Install-command guard scope: hygiene against accidental installs (ADR 0022) |
