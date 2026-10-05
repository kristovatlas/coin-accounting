# Dependency Register

Every direct dependency, runtime or development, in every ecosystem, is listed here **before** it is installed. So is every toolchain binary, GitHub Action and GitHub App the project relies on. See [`ENGINEERING.md` §2.4](ENGINEERING.md#24-adding-a-dependency-vet-before-anything-is-installed) for the vetting procedure.

Transitive packages are covered by the lockfiles, the lockfile policy check and the Socket report. They are listed here only when Socket flags them, or when they have network capability, native code or install-time execution.

## Packages

| Name | Ecosystem | Exact version | Runtime / Dev | Purpose | Alternatives considered | Licence | Network capability? | Native code / install scripts? | Socket result | Added (date, PR) | Approved by |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `pytest` | Python (uv), dev group | 9.1.1 (uploaded 2026-06-19) | Dev | Test runner (ENGINEERING §3) | stdlib `unittest` (used by the M0.1 repository checks); pytest's fixtures, parametrisation and plugin ecosystem (`hypothesis`, coverage) are what §3 is written against | MIT | No by default. Its built-in `pastebin` plugin uploads test output to `bpa.st` only when run with `--pastebin`; disabled for this repo with `-p no:pastebin` (`pyproject.toml`), so the dev-time flow list (THREAT_MODEL §6) stays accurate | No (pure-Python wheel; no `.pth` file in any of the six locked wheels, checked against their hashes on 2026-09-30). Transitive, all pure-Python wheels: `iniconfig` 2.3.0 (MIT), `packaging` 26.3 (Apache-2.0 OR BSD-2-Clause), `pluggy` 1.6.0 (MIT), `pygments` 2.21.0 (BSD-2-Clause); `colorama` 0.4.6 (BSD-3-Clause, Windows only, never installed here) | Socket App on PR #65 (2026-09-30): Supply Chain 87, Vulnerability, Quality, Maintenance, License 100; "Pull Request Alerts" passed with no alerts | 2026-09-30, PR #65 (M0.2) | the human, by merging PR #65 (ENGINEERING §2.4) |
| `hypothesis` | Python (uv), dev group | 6.168.1 (uploaded 2026-09-23) | Dev | Property tests and fuzzing (ENGINEERING §3; T-205, T-501) | hand-written example tests only; no other maintained property-testing library for Python | MPL-2.0 | No | **Native code** (compiled extension; no pure-Python wheel), allowed by ADR 0024. No install scripts. Transitive: `sortedcontainers` 2.4.0 (Apache-2.0, pure Python, uploaded 2021-05-16) | *pending: Socket report on this PR* | 2026-10-01, M0.2 | the human, by merging this PR (ENGINEERING §2.4) |
| `coverage` | Python (uv), dev group | 7.16.1 (uploaded 2026-09-13) | Dev | Coverage floors and ratchet, including E2E subprocess coverage (ENGINEERING §3.3) | stdlib `trace` (line counts only: no branch coverage, subprocess measurement or combining), which can't meet the §3.3 floors | Apache-2.0 | No | **Native code** (the C tracer in the `cp313` wheels; uv installs those ahead of the pure `py3-none-any` wheel), allowed by ADR 0024. **Start-up hook:** every wheel ships `a1_coverage.pth` (identical, sha256 `ef2ed06d…48e8`), which imports coverage only when `COVERAGE_PROCESS_START`/`COVERAGE_PROCESS_CONFIG` is set; allowed by ADR 0025, pinned in `scripts/check_pth.py`. No install scripts. No transitive dependencies. Wheel contents checked against the lock hashes on 2026-10-01 (Linux x86_64/aarch64, musl aarch64, macOS arm64/x86_64, `py3-none-any`) | Socket App on PR #78 (2026-10-01): Supply Chain 95, Vulnerability, Quality, Maintenance, License 100; "Pull Request Alerts" passed with no alerts | 2026-10-01, PR #78 (M0.2) | the human, by merging PR #78 (ENGINEERING §2.4) |
| `ruff` | Python (uv), dev group | 0.16.8 (uploaded 2026-09-16) | Dev | Lint and format, with per-path `banned-api` rules (ENGINEERING §5) | `flake8` plus plugins and `black`: several packages, slower, and no single per-path import-ban config | MIT | No | **Native code**: the whole tool is a Rust binary inside a `py3-none-<platform>` wheel; there is no pure wheel. Allowed by ADR 0024. No install scripts. No transitive dependencies | *pending: Socket report on this PR* | 2026-10-01, M0.2 | the human, by merging this PR (ENGINEERING §2.4) |
| `mypy` | Python (uv), dev group | 2.3.1 (uploaded 2026-08-15) | Dev | Strict typing (ENGINEERING §5) | `pyright` (needs Node at run time) | MIT | No | **Native code** (mypyc-compiled `cp313` wheels; uv installs them ahead of the pure wheel), allowed by ADR 0024. No install scripts. Transitive: **`librt` 0.15.0** (MIT, the mypyc runtime library, **native**) and **`ast-serialize` 0.11.2** (MIT, mypy's AST serializer, **native**), both published by the mypy project and covered by ADR 0024; `mypy-extensions` 1.1.0 (MIT), `pathspec` 1.1.1 (MPL-2.0) and `typing-extensions` 4.16.0 (PSF-2.0), all pure Python | *pending: Socket report on this PR* | 2026-10-01, M0.2 | the human, by merging this PR (ENGINEERING §2.4) |
| `fastapi` | Python (uv), runtime | 0.141.1 (uploaded 2026-07-29) | Runtime | HTTP API and request validation (ADR 0002, architecture §1) | `starlette` alone, with no pydantic and no native code but hand-written validation (ADR 0028, option 2). FastAPI 0.142.x requires `opentelemetry-api` and is avoided until decided (ADR 0028) | MIT | No outbound use: `http.client` only for status phrases. The server socket is uvicorn's | `pydantic-core` 2.46.5 is **native** (Rust; MIT), allowed at runtime by ADR 0028. Pure-Python requirements: `starlette` 1.7.0 (BSD-3-Clause), `pydantic` 2.13.5 (MIT), `anyio` 4.15.1 (MIT), `idna` 3.20 (BSD-3-Clause), `annotated-doc` 0.0.5 (MIT), `annotated-types` 0.8.0 (MIT), `typing-inspection` 0.4.4 (MIT). No install scripts; no `.pth` and no `.data/scripts` in any locked wheel, checked against their hashes on 2026-10-02 | Socket App on PR #104 (2026-10-02): Supply Chain, Vulnerability, Quality, Maintenance, License 100; "Pull Request Alerts" passed with no alerts. Socket scores direct dependencies; the transitive ones, `pydantic-core` included, are in its full report | 2026-10-02, M0.3 | **pending human approval** (ADR 0028) |
| `uvicorn` | Python (uv), runtime | 0.53.0 (uploaded 2026-09-14) | Runtime | ASGI server run in-process by the launcher (architecture §3) | `hypercorn`, `granian` (larger or native). 0.54.0 is inside the cooldown | BSD-3-Clause | **Yes, inbound:** it binds the F1 server socket, on loopback only (architecture §4, §5). No outbound use | Plain `uvicorn`, without `[standard]` (no `uvloop`, `httptools`, `watchfiles`, `websockets`). Requires `click` 8.5.0 (BSD-3-Clause) and `h11` 0.16.0 (MIT), both pure Python. Its reload, worker and CLI paths (the only `subprocess` uses) aren't used. No install scripts; no `.pth` | Socket App on PR #104 (2026-10-02): Supply Chain 98, Vulnerability, Quality, Maintenance, License 100; "Pull Request Alerts" passed with no alerts | 2026-10-02, M0.3 | **pending human approval** (ADR 0028) |

## Proposed, awaiting approval

Step 1 of [ENGINEERING §2.4](ENGINEERING.md#24-adding-a-dependency-vet-before-anything-is-installed) is to justify each dependency. **Nothing below is installed.** Once the human approves this list and the pinned toolchain is installed, each package is resolved with `make propose-*` in its own PR, the Socket report is reviewed, and the human approves the lockfile diff before `make bootstrap`. Versions are picked at that point, at least 7 days old.

**Python, runtime**: `fastapi` and `uvicorn` were approved by the owner on 2026-10-04 (Socket verdict and lockfile diff; PR #104, ADR 0028), and are listed in the table above.

Deliberately **not** proposed, because the stdlib or our own code covers it:
- the RPC client (`http.client`)
- price downloads (`urllib` plus a small SOCKS5 client of our own, instead of `httpx`/`socksio`)
- SQLite and migrations (`sqlite3` + versioned SQL files)
- TOML config (`tomllib`)

**Python, development**

| Package | Purpose | Notes |
|---|---|---|
| `mutmut` | Mutation testing (§3.4) | |

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

**Tools (pinned binaries, M0.2):** `zizmor` and `actionlint` for workflow checks: pinned in the toolchain table below, pending approval.

## Toolchain and non-package downloads

Verified as described in [`ENGINEERING.md` §2.3](ENGINEERING.md#23-all-installs-go-through-socket-firewall-via-the-repos-scripts).

| Artifact | Version | Platform | SHA-256 (committed) | Verification method | Added (date, PR) |
|---|---|---|---|---|---|
| `sfw` (Socket Firewall Free) | 1.15.2 | linux-x86_64, linux-arm64, darwin-arm64, darwin-x86_64 | in `scripts/toolchain.lock` | GitHub release asset digest; Socket publishes no checksums, so this is **trust-on-first-use** | 2026-09-28, M0.1 |
| pnpm (native binary, npm registry tarball `@pnpm/exe.<platform>`) | 12.5.1 | linux-x86_64 and linux-arm64 (glibc builds), darwin-arm64, darwin-x86_64 | sha512 integrity in `scripts/toolchain.lock` | npm registry integrity (computed by the registry at publish time). The package also carries an npm registry signature and a **SLSA v1 provenance attestation** (checked in registry metadata, 2026-10-01; verifying them at install is #15). The binary is a **Rust program with no embedded Node runtime**: it contains Rust standard-library paths and no V8 or Node symbols, so the pinned Node isn't involved. The `pnpm` package itself is only a launcher that fetches this binary with an install script, or downloads one at run time, so it is not used | 2026-09-28, M0.1; native binary 2026-09-30, M0.2 |
| uv | 0.12.17 | as `sfw` | in `scripts/toolchain.lock` | Publisher `sha256.sum`; matches the GitHub asset digest | 2026-09-28, M0.1 |
| Node.js | 24.21.0 (LTS) | same | in `scripts/toolchain.lock` | nodejs.org `SHASUMS256.txt` (GPG check of the `.asc` against the Node release keys: M0.2) | 2026-09-28, M0.1 |
| CPython (python-build-standalone) | 3.13.15 (20260901) | same | in `scripts/toolchain.lock` | Publisher `SHA256SUMS` | 2026-09-28, M0.1 |
| `bitcoind` (regtest only) | 31.1 | same | in `scripts/toolchain.lock` | bitcoincore.org `SHA256SUMS` (builder-signature threshold: M0.2); 31.0 is added for minimum-version tests in M0.2 | 2026-09-28, M0.1 |
| `actionlint` (workflow checker) | 1.7.12 | linux-x86_64, linux-arm64, darwin-arm64, darwin-x86_64 | in `scripts/toolchain.lock` | GitHub release asset digest, **trust-on-first-use against GitHub** (ADR 0026): at pin time it matched the release's `checksums.txt` (same release) and GitHub's SLSA provenance listing (`release.yaml` @ `v1.7.12`; signatures not verified). Published 2026-03-30. Run with `-shellcheck= -pyflakes=` (ADR 0026). Installed by `make lint-tools`, run by `make lint-workflows` | 2026-10-01, M0.2, **pending human approval** |
| `zizmor` (workflow security checker) | 1.30.1 | same | in `scripts/toolchain.lock` | GitHub release asset digest, **trust-on-first-use against GitHub** (ADR 0026): no checksum file is published, and GitHub's SLSA provenance listing (`release-binaries.yml` @ `v1.30.1`) was read but its signatures not verified. Published 2026-09-09. **Network:** its online audits call the GitHub API with a no-permission `ZIZMOR_GITHUB_TOKEN`, in an otherwise empty environment, with `--no-config` (ADR 0026). Installed by `make lint-tools` | 2026-10-01, M0.2, **pending human approval** |
| `osv-scanner` (vulnerability audit) | 2.6.0 | linux-x86_64, linux-arm64, darwin-arm64, darwin-x86_64 | in `scripts/toolchain.lock` | GitHub release asset digest: **trust-on-first-use against GitHub** (ADR 0027). It matches the publisher's `osv-scanner_SHA256SUMS`, which is in the same release, and the release's SLSA provenance (`multiple.intoto.jsonl`) isn't verified yet. Published 2026-09-14. Run with an empty config, `--no-resolve` and an empty environment. Replaces the proposed `pip-audit` (a heavy dependency tree) and `pnpm audit`. Installed by `make audit-tools` | 2026-10-01, M0.2, **pending human approval** |
| Playwright browsers | — | — | — | Pinned with `@playwright/test` (M0.2) | — |

**Approved by the human** on 2026-09-28 in PR #7: the pins above, exactly as committed in `scripts/toolchain.lock`. A change to any pin needs a new approval (ENGINEERING §2.4).

## GitHub Actions and Apps

| Name | Kind (Action / App) | Pinned SHA / permissions | Purpose | Added (date, PR) |
|---|---|---|---|---|
| `actions/checkout` | Action | `3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1, released 2026-07-20); `contents: read`, `persist-credentials: false` | Check out the repo in CI | 2026-09-28, M0.1 |
| Socket GitHub App | App | installed on this repository by the human (checks "Project Report" and "Pull Request Alerts") | Socket report on every PR | present by 2026-09-28 (seen on PR #7) |
| Dependabot | GitHub feature | `.github/dependabot.yml`: `uv`, `npm` (pnpm workspace) and `github-actions`; monthly, one grouped PR per ecosystem, 7-day cooldown. Security updates are a repository setting (the owner's choice) | Dependency update PRs, reviewed under ENGINEERING §2.4 | 2026-10-01, M0.2 |

## Cooldown exceptions

Each exception is version-specific, justified by an exploitable vulnerability, and expires within 7 days ([`ENGINEERING.md` §2.6](ENGINEERING.md#26-updating-dependencies)).

| Package@version | Ecosystem | Reason (advisory, exploitability) | Granted (date, PR) | Expires | Removed (date, PR) |
|---|---|---|---|---|---|
| *(none)* | | | | | |

## Removed dependencies

| Name | Ecosystem | Removed (date, PR) | Reason |
|---|---|---|---|
| *(none yet)* | | | |
