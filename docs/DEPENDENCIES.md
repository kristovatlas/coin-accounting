# Dependency Register

Every direct dependency, runtime or development, in every ecosystem, is listed here **before** it is installed. So is every toolchain binary, GitHub Action and GitHub App the project relies on. See [`ENGINEERING.md` §2.4](ENGINEERING.md#24-adding-a-dependency-vet-before-anything-is-installed) for the vetting procedure.

Transitive packages are covered by the lockfiles, the lockfile policy check and the Socket report. They are listed here only when Socket flags them, or when they have network capability, native code or install-time execution.

## Packages

| Name | Ecosystem | Exact version | Runtime / Dev | Purpose | Alternatives considered | Licence | Network capability? | Native code / install scripts? | Socket result | Added (date, PR) | Approved by |
|---|---|---|---|---|---|---|---|---|---|---|---|
| *(none yet)* | | | | | | | | | | | |

## Proposed, awaiting approval

Step 1 of [ENGINEERING §2.4](ENGINEERING.md#24-adding-a-dependency-vet-before-anything-is-installed) is to justify each dependency. **Nothing below is installed.** Once the human approves this list and the pinned toolchain is installed, each package is resolved with `make propose-*` in its own PR, the Socket report is reviewed, and the human approves the lockfile diff before `make bootstrap`. Versions are picked at that point, at least 7 days old.

**Python, runtime**

| Package | Purpose | Why not stdlib / alternatives | Notes |
|---|---|---|---|
| `fastapi` | HTTP API, request validation (ADR 0002) | stdlib `http.server` has no routing, validation or ASGI. Alternative: **`starlette` alone**, which is smaller and avoids `pydantic-core` (a Rust native wheel), at the cost of hand-written validation | Pulls `starlette`, `pydantic`, `pydantic-core` (native), `anyio`, `typing-extensions`, `annotated-types`, `idna`, `sniffio` |
| `uvicorn` | ASGI server run in-process by the launcher (architecture §3) | No stdlib ASGI server. Alternatives (`hypercorn`, `granian`) are larger or native | Plain `uvicorn`, without `[standard]` (no `uvloop`/`httptools` native extras). Pulls `h11`, `click` |

Deliberately **not** proposed, because the stdlib or our own code covers it:
- the RPC client (`http.client`)
- price downloads (`urllib` plus a small SOCKS5 client of our own, instead of `httpx`/`socksio`)
- SQLite and migrations (`sqlite3` + versioned SQL files)
- TOML config (`tomllib`)

**Python, development**

| Package | Purpose | Notes |
|---|---|---|
| `pytest` | Test runner (ENGINEERING §3) | Plugins load only when named (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`) |
| `hypothesis` | Property tests and fuzzing (T-205, T-501) | |
| `coverage` | Coverage floors, including E2E subprocess coverage (§3.3) | Has an optional C tracer |
| `ruff` | Lint and format, with per-path `banned-api` rules | Native binary wheel (Rust) |
| `mypy` | Strict typing | Compiled (mypyc) wheels |
| `mutmut` | Mutation testing (§3.4) | |
| `pip-audit` | CVE audit of `uv.lock` (`make audit`) | Heavy dependency tree (`requests`, `cyclonedx`…). The alternative, `osv-scanner` (a Go binary pinned like the toolchain), is to be evaluated in M0.2 |

**JavaScript, runtime (bundled into the SPA)**

| Package | Purpose | Notes |
|---|---|---|
| `react`, `react-dom` | UI (ADR 0002) | |
| `cytoscape` | Graph rendering | Must pass the E2E strict-CSP check (inline styles) |
| `cytoscape-dagre` (+ `dagre`) | DAG layout for the UTXO graph | Alternative: `cytoscape-elk` (heavier) |

**JavaScript, development and build**

| Package | Purpose | Notes |
|---|---|---|
| `vite`, `@vitejs/plugin-react` | Build and bundle (writes the shipped bundle, so it is vetted like runtime) | `esbuild`/`rollup` native binaries come in transitively; they are prebuilt, not built at install, so `allowBuilds` stays empty (verify in M0.2) |
| `typescript` | Strict typing | |
| `vitest`, `@vitest/coverage-v8` | Frontend unit tests and coverage | |
| `@playwright/test` | E2E (ENGINEERING §3.1) | Browser downloads are pinned and hash-checked separately |
| `eslint`, `typescript-eslint`, `eslint-plugin-react`, `eslint-plugin-react-hooks` | Lint, including the CSP/XSS and network-sink rules (§5.2) | |

**Tools (pinned binaries, M0.2):** `zizmor` and `actionlint` for workflow checks.

## Toolchain and non-package downloads

Verified as described in [`ENGINEERING.md` §2.3](ENGINEERING.md#23-all-installs-go-through-socket-firewall-via-the-repos-scripts).

| Artifact | Version | Platform | SHA-256 (committed) | Verification method | Added (date, PR) |
|---|---|---|---|---|---|
| `sfw` (Socket Firewall Free) | 1.15.2 | linux-x86_64, darwin-arm64, darwin-x86_64 | in `scripts/toolchain.lock` | GitHub release asset digest; Socket publishes no checksums, so this is **trust-on-first-use** | 2026-09-28, M0.1 |
| pnpm (standalone) | 12.5.1 | same | in `scripts/toolchain.lock` | GitHub release asset digest (no publisher checksums) | 2026-09-28, M0.1 |
| uv | 0.12.17 | same | in `scripts/toolchain.lock` | Publisher `sha256.sum`; matches the GitHub asset digest | 2026-09-28, M0.1 |
| Node.js | 24.21.0 (LTS) | same | in `scripts/toolchain.lock` | nodejs.org `SHASUMS256.txt` (GPG check of the `.asc` against the Node release keys: M0.2) | 2026-09-28, M0.1 |
| CPython (python-build-standalone) | 3.13.15 (20260901) | same | in `scripts/toolchain.lock` | Publisher `SHA256SUMS` | 2026-09-28, M0.1 |
| `bitcoind` (regtest only) | 31.1 | same | in `scripts/toolchain.lock` | bitcoincore.org `SHA256SUMS` (builder-signature threshold: M0.2); 31.0 is added for minimum-version tests in M0.2 | 2026-09-28, M0.1 |
| Playwright browsers | — | — | — | Pinned with `@playwright/test` (M0.2) | — |

## GitHub Actions and Apps

| Name | Kind (Action / App) | Pinned SHA / permissions | Purpose | Added (date, PR) |
|---|---|---|---|---|
| `actions/checkout` | Action | `3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1, released 2026-07-20); `contents: read`, `persist-credentials: false` | Check out the repo in CI | 2026-09-28, M0.1 |
| Socket GitHub App | App | installed on this repository by the human (checks "Project Report" and "Pull Request Alerts") | Socket report on every PR | present by 2026-09-28 (seen on PR #7) |
| Dependabot | GitHub feature | configured in M0.2 (monthly, grouped, 7-day cooldown) | Dependency update PRs | M0.2 |

## Cooldown exceptions

Each exception is version-specific, justified by an exploitable vulnerability, and expires within 7 days ([`ENGINEERING.md` §2.6](ENGINEERING.md#26-updating-dependencies)).

| Package@version | Ecosystem | Reason (advisory, exploitability) | Granted (date, PR) | Expires | Removed (date, PR) |
|---|---|---|---|---|---|
| *(none)* | | | | | |

## Removed dependencies

| Name | Ecosystem | Removed (date, PR) | Reason |
|---|---|---|---|
| *(none yet)* | | | |
