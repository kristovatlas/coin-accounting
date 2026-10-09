# Engineering Practices — Coin Accounting

> **Binding once approved.** From the first line of code, these practices apply to every contributor, human or AI agent. Changing them requires a PR that edits this file and gets human approval; under cruise mode with autopilot ([ADR 0031](adr/0031-autopilot.md)), a change that keeps every control merges after the AI review panel instead. A change that weakens a control always needs an ADR, the human's approval and an update to [`THREAT_MODEL.md`](THREAT_MODEL.md).

| | |
|---|---|
| Version | 0.2.35 |
| Last updated | 2026-10-09 |
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
| Package manager | **pnpm ≥ 12**, version pinned in `scripts/toolchain.lock` (§2.3) and checked by `engines.pnpm` in `package.json`. There is **no `packageManager` field**: pnpm 12 resolves that pin against the registry on every command, even `pnpm --version`, outside `sfw`, and writes it into `pnpm-lock.yaml` (#51). The lockfile check (§2.5) rejects the field. `pmOnFail: error`, so pnpm never downloads a different version of itself. npm/yarn are not used |
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
| `.pth` files | Wheels can ship `.pth` files that run on every interpreter start. `make bootstrap` runs `scripts/check_pth.py` right after installing and fails on any `.pth` file not on its allowlist. Two are allowed. One is uv's own `_virtualenv.pth`, and only with uv's exact content, alongside the `_virtualenv.py` it imports with uv's hash, both regular files. Nothing else that `import _virtualenv` could load may exist: no `_virtualenv` package, no other `_virtualenv.*` file, and no cached bytecode for it at all, since a `.pyc` with a copied header would run anything. `make bootstrap` deletes those caches before the check, and the venv's Python rebuilds them from uv's verified source. Names are compared case-insensitively. No installed package's `RECORD` may claim any of these (PR #74 review, rounds 1–2). The other is coverage.py's `a1_coverage.pth`, which starts subprocess measurement only when `COVERAGE_PROCESS_START`/`COVERAGE_PROCESS_CONFIG` is set (§3.3). It is allowed only with its pinned sha256, as a regular file, claimed by coverage's own `RECORD` with that hash, and with nothing else providing the `coverage` module (ADR 0025). CI runs it too once CI installs dependencies (#44) |

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
  | `make e2e-tools` | The pinned, verified headless Chrome for the E2E tests (ADR 0033) |
  | `make lint-tools` / `make lint-workflows` | Install the pinned actionlint and zizmor; check the workflows with them (§2.7, ADR 0026) |
  | `make audit` | The pinned `osv-scanner` binary (§2.3, `make audit-tools`) against `uv.lock` and `pnpm-lock.yaml`. It sends package names and versions to `api.osv.dev` (THREAT_MODEL §6), with an empty config from outside the repository, `--no-resolve` and an empty environment (ADR 0027). One tool for both ecosystems, so no `pip-audit` dependency tree |
  | `make test` | The backend unit and regtest integration tests (`pytest`, with only the named plugins and the socket guard, §3.2) under `coverage`, then the §3.3 floors: 85 % overall, 95 % for each of `chain/`, `tax/`, `doxx/` that exists. Needs the pinned `bitcoind` (`make test-tools`); the harness in `e2e/harness/` verifies it before each run |
  | `make lint` | `ruff check`, `ruff format --check` and `mypy --strict` on `backend/` (§5.1). The older `scripts/` aren't covered yet |

  Contributors, AI agents and CI all use these targets. CI jobs call `make toolchain` and `make bootstrap`, never raw installers.
- **No silent fallback:** if `sfw` is missing, not the pinned version, or fails to start, the scripts **stop with an error**. They never drop through to an unwrapped install. Whether `sfw` fails open when the Socket API is unreachable must be tested at setup. If it does, the wrapper detects that and stops **(verify at setup)**.
- **Fetch-and-run commands are banned:** `npx`, `pnpm dlx`, `pnpm exec` of packages not in the lockfile, `uvx`/`uv tool run`, `pip install`, `curl … | sh`, the `pre-commit` framework (it clones and builds hook environments). The same applies to IDE and agent configuration, e.g. `.mcp.json` servers started with `npx -y`. Tools we need become locked dev dependencies. Git hooks, if any, are `repo`-local scripts that call locked tools.
- **Verifying before use:** every install target first runs `toolchain.py verify sfw pnpm uv node python`, with a host interpreter the caller can't override, and then calls the tools by absolute path. Verification covers **every file** of each install (a digest recorded at install time), not just the entry point, so pnpm's native binary, Node and the Python that uv uses are checked too, bytecode caches included. **Installed trees are read-only**, so running a tool can't write into its own install, and verification also fails on a writable tree. `make toolchain` re-applies read-only without reinstalling, and `PYTHONDONTWRITEBYTECODE` is a second layer. A missing, modified, writable or moved install stops the command; there's no fallback to a host `pnpm`/`uv`.
  - **Limits:** read-only stops accidents, not someone who can `chmod`. Root ignores the bits, so don't run `make` with `sudo`. A symlinked `.toolchain`, `.toolchain/bin`, tool directory or version directory is refused before anything is walked, changed or linked (symlinks inside an installed tree are allowed and recorded in the digest), and a directory that can't be read fails verification instead of being skipped. A download failure (network, TLS) is reported as one, with its cause. If `.toolchain` was created by another user, `make toolchain` says so instead of failing with a traceback.
  - **Removing the toolchain:** `rm -rf .toolchain` and `git clean -fdx` need `chmod -R u+w .toolchain` first.
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
  | pnpm | the per-platform native binary (`@pnpm/exe.<platform>` npm registry tarball) checked against the registry's sha512 integrity, committed in the lock. The `pnpm` launcher package isn't used: it fetches or downloads the binary itself (registry signature and provenance: M0.2) |
  | uv | Release artifacts checked against the publisher's checksums, and our committed SHA-256 |
  | Node.js | `SHASUMS256.txt` verified against the Node release keys |
  | Python | Pinned interpreter build checked against a committed SHA-256 |
  | `bitcoind` (regtest) | `SHA256SUMS` plus a threshold of builder signatures (pinned `guix.sigs` builder keys), minimum and latest supported versions |
  | actionlint, zizmor | Committed per-platform SHA-256 of the GitHub release asset: trust-on-first-use against GitHub, like `sfw`. At pin time it was cross-checked against actionlint's `checksums.txt` and GitHub's SLSA provenance listing; the provenance signatures aren't verified yet (ADR 0026) |
  | osv-scanner | Committed per-platform SHA-256 of the GitHub release binary: trust-on-first-use against GitHub. It matches the publisher's `osv-scanner_SHA256SUMS`, which is in the same release, and its SLSA provenance isn't verified yet (ADR 0027) |
  | Playwright browsers | Pinned `@playwright/test` version, and the one browser the E2E tests use, Chrome for Testing's headless shell at the build that version expects, pinned in `scripts/toolchain.lock` with a committed per-platform SHA-256 (trust-on-first-use against Playwright's CDN; at pin time each archive matched Google's `chrome-for-testing-public` copy; ADR 0033). The `e2e-tools` target installs it like the other pins (fails closed on a missing or wrong hash); the E2E tests (M0.3 H4, part 2) launch it through `executablePath`, after verifying it, so Playwright's own downloader never runs. On Linux it needs the host's NSS, ATK, X11, GBM and ALSA libraries: host prerequisites, like the host `python3`, which this repository doesn't install (GitHub's Ubuntu runners have them) |

- **Enforcement:** a CI check (`scripts/check-install-commands`) scans the `Makefile`, `scripts/`, `.github/workflows/` and config files for install or fetch-and-run commands without the `sfw` wrapper. It can be bypassed by obfuscation, so it is **hygiene, not a security boundary** (ADR 0022: the Claude Code guard and this check catch accidental or habitual installs; deliberate evasion is accepted risk R-8), and it allowlists documentation files that quote the banned commands. `AGENTS.md` repeats the rule for agents.

### 2.4 Adding a dependency: vet before anything is installed

The same process applies to a new direct dependency, and to a version bump of an existing one:

Third-party code is never copied into the repository (a vendored package, a minified bundle, a file with another project's licence header); it comes in through this process, so Socket and the lockfile diff see it (ADR 0031).

1. **Justify it.** Why can't the standard library or ~100 lines of our own code do the job? Could a package we already use do it?
2. **Resolve only.** Run `make propose-js`/`make propose-py`. This updates the manifest and lockfile without installing anything, and without building sdists or loading a `.pnpmfile` (step 7).
3. **Review the Socket verdict** for every new or changed package in the lockfile diff. Use the Socket GitHub App's report on the draft PR (which contains only the manifest/lockfile change), or the package's socket.dev page. Look for install scripts, network or filesystem access, obfuscated code, telemetry, new maintainers and typosquat signals.
4. **Check its health:** maintainers, release history, open advisories, download base, licence, transitive dependency count.
5. **Record it** in [`DEPENDENCIES.md`](DEPENDENCIES.md).
6. **Human approval.** The human explicitly approves the dependency in the PR. AI agents may propose dependencies, never approve them.
7. **Only then install.** Approval is the human's merge to `main`, compared against `refs/remotes/origin/main`.
   - `make bootstrap` installs only dependency files that match it: the manifests and lockfiles, plus config that changes installs or runs code during them (`.pnpmfile.*`, `.npmrc`, `uv.toml`, `pnpm-workspace.yaml`, `.python-version`, `.node-version`). Untracked files count even if a gitignore would hide them.
   - `scripts/toolchain.py install` (behind `make toolchain`/`make test-tools`/`make e2e-tools`) refuses pins in `scripts/toolchain.lock` that aren't on `main`, including when run directly.
   - Step 2 (`make propose-*`) refuses unapproved install config, because resolving follows it too. It resolves with `--no-build`, and pnpm is set to `ignorePnpmfile`, so no package or hook code runs while resolving **(verify at setup)**.
   - To use an approved change before it is merged, **the human** adds `DEPS_APPROVED=1` to the make command line. CI does the same, explicitly in its workflow file, so dependency PRs can be tested before approval on a throwaway machine (from M0.2; THREAT_MODEL §5.6.1, T-608). Agents never set it (AGENTS.md); the Claude Code guard blocks it, while other agents are bound by the AGENTS.md rule alone.
   - GNU make treats a variable in an inherited `MAKEFLAGS` as a command-line one, so **never put `DEPS_APPROVED` in `MAKEFLAGS`**, a shell profile or agent settings.
   - The gate checks the files on the current branch; it does not protect against a malicious branch. Don't run `make` targets on branches you don't trust.

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
- the pnpm lockfile is read in full, as one document: a second document fails the check (some scanners read only the first, which would hide entries from them)

**Status (M0.2):** `scripts/check_lockfiles.py` runs in `make check` and CI, on the pinned Python once it verifies, otherwise the host's (3.11+), and its tests run on that interpreter too. It checks every `uv.lock` entry: source, a file URL of exactly PyPI's shape (`https://files.pythonhosted.org/packages/xx/yy/<60 hex>/<file>`, so no query, fragment, `%`-escape, backslash or dot segment), a file name from that URL matching the entry's name and version, sha256 and age. Declared Python dependencies without a `uv.lock` fail. It also checks our `package.json` files (lifecycle scripts, `configDependencies` and `packageManager`, including escaped keys) and `pnpm-workspace.yaml`:
- no `configDependencies` and no backslashes
- no `pnpmfile`, `globalPnpmfile`, `sharedWorkspaceLockfile`, `gitBranchLockfile`, `lockfile` or `lockfileDir` setting
- no non-ASCII text outside full-line comments, and no byte-order mark (it would hide a first-line key from the text checks)
- none of these YAML constructs: explicit keys (`?`), tags, anchors, aliases, merge keys, document markers, or a flow collection at the start of a line. This is a text heuristic, not a YAML parser; the approval gate and the owner's review of `pnpm-workspace.yaml` cover what it doesn't model
- `ignorePnpmfile: true` must be present, exactly once

A `.pnpmfile.*` in any letter case anywhere in the tree fails. **`pnpm-lock.yaml`** (M0.3) is the only pnpm lockfile allowed; any other `pnpm-lock*.yaml`, in any letter case and anywhere, fails. It is read in full and strictly, line by line, with no YAML library:
- lockfile version `9.0`, one document, ASCII only, no tabs, and none of the YAML constructs above
- only the top-level keys pnpm writes for registry packages (`lockfileVersion`, `settings`, `importers`, `packages`, `snapshots`), and only its two default settings
- no URL, git, tarball, `link:`, `file:`, `workspace:` or directory source anywhere in the file, and no alias (a version naming another package), direct or transitive
- every `packages`, `snapshots` and `importers` line has one of the shapes pnpm 12 writes, every snapshot and importer reference names a checked `packages` entry, and every `packages` entry has a snapshot
- every `packages` entry has exactly one resolution, of exactly the form `{integrity: sha512-…}`, and no `@jsr/` scope; no resolution anywhere else. **Known limit:** a bare integrity means the registry configured for the package's scope, so the check relies on the registry settings for `registry.npmjs.org`; pinning them is #36 and #93
- every package's publish time, read from **`pnpm-lock.times.json`**, is at least 7 days old. `make propose-js` records those times with the pinned pnpm under `sfw` (`scripts/npm_publish_times.py`), so `make check` stays offline. **Known limit, accepted by the owner (2026-10-05), as for `uv.lock`:** a hand-edited times file could back-date a package; every lockfile change still needs the owner's approval with the Socket report (§2.4).

The `scripts/check-lockfiles` wrapper and the Makefile (its `SYS_PYTHON`, which also runs every toolchain verification) find the host `python3` on `PATH` skipping `.toolchain/bin` and the pinned binary, so the pinned Python never verifies itself; it runs only after that host interpreter has verified it. There are no cooldown exceptions yet, so the check allows none: recording one (§2.6) means extending the check in the same PR. **Known limit, accepted by the owner (PR #63):** the age check trusts the `upload-time` recorded in `uv.lock`, so a hand-edited lockfile could back-date a package. Every lockfile change still needs the owner's approval with the Socket report (§2.4).

### 2.6 Updating dependencies

- Updates come in **deliberate batches** (at most monthly, plus urgent security fixes), never as a side effect of other work.
- Dependabot runs on a monthly schedule with grouped updates and a 7-day cooldown. It covers the `npm`, `uv` and `github-actions` ecosystems, the last so that SHA-pinned actions get updates. Dependabot changes **manifests as well as lockfiles**. Its PRs go through the §2.4 review in full. Configured in `.github/dependabot.yml` (M0.2); whether Dependabot reads pnpm 12's multi-document lockfile is checked with the first JavaScript dependency (§2.5).
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
  - `zizmor` and `actionlint` run on workflow files (they catch impostor-commit pins and unsafe patterns). They're pinned binaries (§2.3), installed by `make lint-tools` and run by `make lint-workflows`. actionlint runs without its shellcheck/pyflakes integrations; both run with an otherwise empty environment, and zizmor runs its online audits (impostor-commit among them) over the whole repository against the GitHub API, with a dedicated no-permission `ZIZMOR_GITHUB_TOKEN` and no config file. `scripts/check_repo_files.py` rejects linter config files and `zizmor: ignore` comments in any tracked YAML file, so a PR can't switch a check off (ADR 0026). CI runs them once the pins are on `main` (#94)
  - `permissions: contents: read` by default, and the repository default token is read-only
  - `persist-credentials: false` on checkout
  - no `pull_request_target`
  - job timeouts set
  - "Allow GitHub Actions to create and approve pull requests" is disabled
  - PRs from outside contributors' forks need approval before workflows run, since the repository is public
- **Secrets:** CI needs none. The Socket GitHub App is used rather than the Socket CLI, which would need an API token.
- **Caches:** no dependency caches in CI (`sfw` can't inspect cached artifacts). On developer machines, pnpm `storeDir` and uv `cache-dir` are project-local, so packages vetted for other projects are never reused unchecked.
- **Checks on every PR:**
  - `make audit` (`osv-scanner`; the CI step comes once the pin is on `main`, #94)
  - the lockfile policy check
  - the install-command check
  - the Socket App report
  - tests and coverage on **Linux and macOS**: the `tests` job (`make test`, `make lint`, the check that the E2E browser pin matches the installed Playwright, and `make e2e`), on the `pull_request` trigger only, as T-608 requires
  - a **reproducible frontend build**: build twice in the same pinned environment and compare the normalized `dist/` output
- **Branch protection on `main`:**
  - PRs only, with all required checks green
  - stacked PRs are merged with merge commits
  - **only the human merges**, or explicitly tells an agent to merge. Agents act with the owner's GitHub credentials, so this is a **procedural** rule, not a technical one: user decision, 2026-09-27; THREAT_MODEL T-605. The exception is cruise mode's gate with autopilot (ADR 0030, ADR 0031, R-11, R-12): it merges every PR the review panel cleared (its `review-panel` commit status) except dependency, lock and install-config files, ADRs and the architecture baseline, and the agents' own controls, including the test socket guard and every `conftest.py` (ADR 0031 §3)
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

A pytest hook (`pytest_configure` in `backend/tests/conftest.py`, before test modules are collected, until `pytest_unconfigure`) patches `socket.socket.connect`, `connect_ex`, `sendto` and `sendmsg`, `socket.getaddrinfo` and the legacy resolver calls. Every blocked attempt is recorded, so a test fails even if the code under test swallows the error, and attempts outside any test fail the session. It allows only:
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
- Floors are minimums, not targets. Totals are compared at two decimal places (`precision = 2`): coverage.py rounds before comparing, so at its default of 0, 94.6 % would pass a 95 % floor.
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

**Test audits:** at each milestone close, and at least monthly while coding is active, a test-audit pass reviews the suite against these rules plus the mutation report. It deletes or strengthens weak tests, and its findings go into the milestone PR. An AI reviewer may do a first pass; a human approves the result (under autopilot, a milestone-closing PR carrying the audit goes to the human, ADR 0031).

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
  - storage format changes, or a change in how chain data is obtained from the node. A migration that implements PLAN §2's data model (or the architecture) is not a storage format change by itself; where the DB lives or is protected (secrets stored in it included), data outside that model, a PLAN change that adds, removes or redefines a kind of stored data, deleting or altering data that can't be recomputed, and loosening a schema rule that a THREAT_MODEL mitigation relies on are ([ADR 0038](adr/0038-schema-changes-within-the-data-model.md))
  - weakening any control in this document or the threat model
  - changes to the architecture diagram
  - supported platform changes

### 4.2 Architecture diagram

- `docs/architecture.md` holds the Mermaid component diagram and the data-flow diagram, reviewed by the human.
- The accepted ADR that last changed the diagram records the file's SHA-256 in its front matter. A CI check fails if the current file doesn't match that hash. The check proves the diagram and ADR are **consistent**. **Approval** is the human's merge of that PR.
- Code that contradicts the diagram is a bug. Either fix the code, or change the diagram through an ADR.

### 4.3 Threat model

- Any PR touching a boundary, asset, store, network flow (including build-time flows), dependency or tax rule updates `THREAT_MODEL.md` in the same PR: statuses, evidence links, changelog. **In cruise mode** ([ADR 0030](adr/0030-cruise-mode.md)), a `/cruise` run's feature PRs leave all three to the run's milestone-closing PR, which merges through the gate like a slice (ADR 0031).
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
| Import edges and capabilities (network, `webbrowser`, `subprocess`, `ctypes` (launcher only, for `prctl`), filesystem, clock, `importlib`/`__import__`) follow the per-path rules in [architecture §2](architecture.md#2-module-structure-dependency-rules-and-capability-rules) | T-305, T-402, purity of `tax/`/`doxx/`/`domain/` | `scripts/check-architecture` (custom AST check, no dependency) + ruff `banned-api` per path. Hygiene, not a security boundary |
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
  - commit to `main` directly or merge PRs unless the human explicitly says so (§2.7). The one exception is cruise mode with autopilot, through `main`'s copy of `scripts/cruise_merge.py` only (ADR 0030, ADR 0031)
  - add MCP servers or tools that fetch and run packages
- **Transparency.** Agent-authored commits carry a `Co-Authored-By` trailer. PR descriptions state what was verified (commands run, tests added) and what wasn't.
- **Review.** Every agent PR gets human review, except one the gate merges in cruise mode with autopilot (ADR 0030, ADR 0031), in a `/cruise` run or not. Independent AI reviews (e.g. a second model) are encouraged for design docs and security-relevant code. Their findings are verified before being acted on, not applied blindly. Review output and the triage decisions are posted as PR comments, as a record.
  - **The review panel** (`/review-panel #N`, [ADR 0020](adr/0020-review-panel.md)) does the AI review:
    - Rounds of four reviews (Opus 5.5 and Codex gpt-5.6-sol, security and functional) find issues, and the panel fixes valid P1s and files issues for the rest.
    - When a round is clean and CI is green, the **tripwire** checks the final commit: a mechanical scan (`scripts/tripwire.py`, from `main`'s copy) plus a separate Opus check for malicious patterns. Examples: process or network use in tests, weakened or deleted guards and tests, new hosts, obfuscated code, dependency or agent-instruction changes. It is a heuristic that points the human's attention, not a guarantee.
    - The flags are posted for the human (after the mechanical secret scan, `scripts/secret_scan.py`), and set as the advisory `tripwire` commit status on that exact commit. The hand-off names the SHA, and a later push has no status until the tripwire runs again.
    - **The human merges**, looking carefully at anything flagged. The panel never merges; afterwards it deletes the branch and cleans up.
    - Severity rules and the round limit ([ADR 0023](adr/0023-review-panel-refinements.md)): fail-safe process edge cases and "an AI could be steered" findings are not P1. After round 5, the human decides each remaining P1 in a plain-language walkthrough: downgrade, keep or accept.
  - **Cruise mode** ([ADR 0030](adr/0030-cruise-mode.md); operator guide [`docs/cruise-mode.md`](cruise-mode.md)). It applies while `PROCESS_MODE` on `main` is `cruise`; `standard` restores the rules above unchanged.
    - **Panel profile:**
      - two reviewers, or all four on risky paths and `chain/`/`tax/`/`doxx/`
      - later rounds review only the fix, with at most 2 rounds
      - a P1 is only a confirmed Critical/High, a broken or flaky test, a real leak, or wrong tax figures
      - one comment per round
    - **`/cruise <scope>`** works through a PLAN scope. With autopilot (ADR 0031) the agent starts the panel on every PR it opens, and the mechanical gate `scripts/cruise_merge.py` (`main`'s copy, with a separate repository-scoped token) merges every PR the review panel cleared except dependency files, ADRs and the architecture baseline, and the agents' own controls. Those, and anything else the gate refuses, go to the human as a draft.
    - **Docs:** each run ends with a milestone-closing PR, merged through the gate, holding the threat-model and changelog updates.

## 7. Workflow

- **Branches:** `main` is always releasable. Work happens on short-lived branches (`<type>/<topic>`, e.g. `feat/scan-jobs`, `docs/adr-0003`).
- **PRs:**
  - small and focused
  - opened as **drafts** for human review on GitHub. The exception is cruise mode with autopilot: the agent opens every PR it means the gate to merge ready for review, in a `/cruise` run or not, and the gate merges only non-draft PRs (ADR 0030, ADR 0031). A PR the gate would refuse is still opened as a draft
  - stacked PRs are allowed and merged with merge commits
  - each PR description lists the affected threat IDs and ADRs
- **Commits:** imperative subject ≤ 72 chars; the body explains *why*.
- **Docs travel with code:** PLAN, threat model, ADRs, diagram and `DEPENDENCIES.md` are updated in the same PR as the change that affects them. In cruise mode, a `/cruise` run's slices leave the threat-model and changelog updates, and PLAN progress, to the run's milestone-closing PR (ADR 0030).

## 8. Definition of Done

A change is done only when:

- [ ] Behaviour is covered by an **E2E test** (for user-visible features) and by unit/integration tests where logic warrants them
- [ ] Coverage floors hold; the PR mutation run holds for changed `tax/`/`doxx/` files; CI is green on Linux and macOS
- [ ] Tests comply with §3.5, with no slop
- [ ] `THREAT_MODEL.md` statuses and evidence links are updated (or explicitly "no change"). In cruise mode, this happens in the run's milestone-closing PR (ADR 0030)
- [ ] ADR written or updated if §4.1 applies; the architecture diagram still matches
- [ ] Any new dependency went through §2.4, is recorded in `DEPENDENCIES.md`, and has human approval
- [ ] No new network flow outside THREAT_MODEL §6; the socket guard stays green
- [ ] User-facing docs updated where behaviour changed
- [ ] The human merged the PR, or in cruise mode the gate did (ADR 0030)

## 9. Enforcement summary

| Practice | Enforced by |
|---|---|
| Cooldowns, exotic-source bans, no build scripts, wheels-only, no auto-install/auto-download | Tool config (§2.1, §2.2) + lockfile policy check (§2.5, `scripts/check-lockfiles`) |
| Vet before install | `make propose-*` (lockfile-only) + Socket App report + human approval |
| Socket Firewall on all installs | `make` targets wrapping `sfw` with no fallback + install-command check (hygiene) |
| Vulnerability audits | `make audit` on every PR + weekly clean-cache job |
| Non-package downloads | Per-artifact verification (§2.3) |
| Actions hardening | `zizmor` + `actionlint` + repository settings |
| Coverage floors and ratchet | CI (`coverage.py`, `vitest`, merge-base comparison) |
| Mutation budget | Per-PR targeted run + weekly full run |
| Banned APIs, float ban, CSP/XSS rules | ruff / AST check / `FloatOperation` trap / ESLint / bundle scan / E2E CSP check |
| Module import edges and capability rules | `scripts/check-architecture` (architecture §2) |
| No symbolic links or submodules in the tree | `scripts/check-repo-files` (ADR 0023) |
| ADR immutability, diagram hash | CI checks |
| Threat model / ADR / DEPENDENCIES updates | PR template checklist + human review; under autopilot (ADR 0031), threat-model and DEPENDENCIES updates are reviewed by the AI panel (the Opus tripwire's "binding-document control" flag goes to the human), and ADRs stay with the human |
| Test-slop rules | Partly automated (§3.5) + review checklist + periodic test audit |
| Only the human merges, except through cruise mode's gate (ADR 0030, autopilot ADR 0031) | Procedural (Documented, T-605); the gate's conditions are mechanical (`scripts/cruise_merge.py`, R-11) |
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
| 2026-09-28 | 0.2.6 | PR #7 review round 4: `bootstrap` installs only merged dependency changes unless the human sets `DEPS_APPROVED=1`; toolchain verification covers every installed file and Node |
| 2026-09-28 | 0.2.7 | PR #7 review round 5: the approval gate covers toolchain pins and install-affecting config and accepts only a command-line approval; Python is verified, bytecode included |
| 2026-09-28 | 0.2.8 | CI passes `DEPS_APPROVED=1` explicitly so dependency PRs are tested before approval (user decision, #44; THREAT_MODEL §5.6.1) |
| 2026-09-28 | 0.2.9 | PR #7 review round 6: approval checked against `refs/remotes/origin/main`, ignored files included; the toolchain installer checks its own pins; `propose-*` refuse unapproved install config, resolve with `--no-build` and `ignorePnpmfile`; the `MAKEFLAGS` and untrusted-branch limits stated |
| 2026-09-28 | 0.2.10 | PR #7 review round 7: the `DEPS_APPROVED` rule is stated for all agents in AGENTS.md; only Claude Code has a technical block |
| 2026-09-28 | 0.2.11 | Review process: the `/review-panel` skill and the tripwire (ADR 0020); the human still merges |
| 2026-09-29 | 0.2.12 | ADR 0023: review severity rules and the round-limit walkthrough; no symlinks or submodules in the repository (`scripts/check_repo_files.py`) |
| 2026-09-30 | 0.2.13 | M0.2: installed toolchain trees are read-only, so running a tool (the pinned Python writes `.pyc` files) can't change what `make require-toolchain` verifies |
| 2026-09-30 | 0.2.14 | M0.2: pnpm is pinned as its native binary (`@pnpm/exe.<platform>`); the launcher package would fetch or download it itself |
| 2026-09-30 | 0.2.15 | M0.2: the lockfile policy check (`scripts/check-lockfiles`) runs in `make check` and CI; the pnpm part lands with the first JavaScript dependency, and a pnpm lockfile fails until then |
| 2026-10-01 | 0.2.16 | M0.2: the `.pth` allowlist check exists (`scripts/check_pth.py`, run by `make bootstrap`); pytest's pastebin opt-out is pinned by a test |
| 2026-10-01 | 0.2.17 | M0.2 verify at setup: no `packageManager` field (pnpm 12 resolves it from the registry on every command, #51); the lockfile check rejects it |
| 2026-10-01 | 0.2.18 | M0.2: the `.pth` allowlist also admits coverage.py's own `a1_coverage.pth`, pinned by content (ADR 0025, PR #78) |
| 2026-10-01 | 0.2.19 | §2.5: the lockfile check is hardened (#71, #72): file-name binding, declared dependencies without `uv.lock`, escaped keys, hook and lockfile settings, `ignorePnpmfile` required, any-case `.pnpmfile`, nested and branch pnpm lockfiles; after PR #81 review: `lockfile`/`lockfileDir` and indirect YAML key syntax rejected, exact PyPI URL shape only, any-case lockfile names; the pinned Python is verified before `make check` or the wrapper uses it; the accepted upload-time limit is recorded |
| 2026-10-01 | 0.2.20 | M0.2: actionlint 1.7.12 and zizmor 1.30.1 pinned (pending approval; ADR 0026); `make lint-tools` / `make lint-workflows`; actionlint without its shellcheck/pyflakes integrations; zizmor with its online audits and a dedicated no-permission token (§2.3, §2.7) |
| 2026-10-01 | 0.2.21 | §2.3 (#69, #67): read-only trees are verified on use and re-applied by `make toolchain`; their limits (root, `chmod`, cleanup); pnpm described as a native binary; after PR #83 review: a symlinked `.toolchain`, `.toolchain/bin`, tool or version directory is refused before any walk, chmod, removal or link, an unreadable directory fails verification instead of being skipped, permission errors anywhere in `make toolchain` get the same message, and download failures keep their own |
| 2026-10-01 | 0.2.22 | §2.6: Dependabot configured (`.github/dependabot.yml`) |
| 2026-10-01 | 0.2.23 | `make audit` uses a pinned `osv-scanner` binary (pending approval; ADR 0027) instead of `pip-audit` + `pnpm audit`: trust-on-first-use against GitHub, run with an empty config, `--no-resolve` and an empty environment; `check_repo_files` rejects a committed `osv-scanner.toml` |
| 2026-10-02 | 0.2.24 | M0.3: `make test` (pytest with the socket guard, coverage floors) and `make lint` (ruff, mypy --strict) for `backend/`; agents may run both |
| 2026-10-02 | 0.2.25 | M0.3: the regtest harness (`e2e/harness/regtest.py`); `make test` runs the integration tests and needs `make test-tools` |
| 2026-10-03 | 0.2.27 | Cruise mode (ADR 0030): a switchable faster process. It adds a lighter review-panel profile, gated automatic merges (`scripts/cruise_merge.py`), `/cruise` milestone loops, and threat-model and changelog updates in a milestone-closing PR (§4.3, §6, §8); `PROCESS_MODE` = `standard` restores the previous rules |
| 2026-10-04 | 0.2.28 | §5.2: `ctypes` is its own capability in `scripts/check_architecture.py`, allowed only in `launcher.py` (for `prctl(PR_SET_DUMPABLE, 0)`, architecture §1); `storage/volume.py` no longer gets it with `subprocess` |
| 2026-10-04 | 0.2.29 | §2.7: CI's `tests` job runs `make test` and `make lint` on every PR, on Linux and macOS, passing `DEPS_APPROVED=1` as decided in #44 (T-608) |
| 2026-10-05 | 0.2.31 | Autopilot (ADR 0031): §6 the agent starts the review panel and the gate merges every PR the panel cleared except dependency and install files, ADRs and the architecture baseline, and the agents' own controls; §4.3 the milestone-closing PR merges through the gate. (0.2.30 was skipped.) |
| 2026-10-06 | 0.2.32 | §2.5: the pnpm lockfile check lands with the first JavaScript dependencies (M0.3): a strict full read of `pnpm-lock.yaml` (registry-only sources, one sha512 integrity per package, no foreign keys or YAML constructs) and the 7-day cooldown from publish times that `make propose-js` records in `pnpm-lock.times.json` (owner decision: recorded at propose time, same back-dating limit as `uv.lock`). Known limit: a bare integrity means the registry configured for the package's scope, so `registry.npmjs.org` relies on the registry settings until #36 and #93 pin them; the `@jsr/` scope and `jsr:` specifiers fail |
| 2026-10-06 | 0.2.33 | §2.3: the E2E browser pin: Chrome for Testing's headless shell 153.0.8010.12 (the build `@playwright/test` 1.63.0 uses) in `scripts/toolchain.lock`, a safe zip extractor in `toolchain.py`, and the `e2e-tools` target (M0.3 H4) |
| 2026-10-06 | 0.2.34 | §3.1: the first E2E test (M0.3 H4). `make frontend` builds the production bundle, which the launcher reads into memory at start-up (`api/` has no filesystem access; architecture §2, ADR 0034) and the app serves under its CSP. `make e2e` starts a regtest node and the real launcher (`e2e/harness/app_under_test.py`), and drives the pinned headless Chrome through the launch file, the session claim, the status and Quit, failing on any CSP violation. CI's `tests` job runs it on Linux and macOS |
| 2026-10-09 | 0.2.35 | §4.1: what "storage format changes" covers for user-DB migrations, per ADR 0038 (#232): a migration within PLAN §2's data model needs no ADR of its own; the cases that still do are listed there |
