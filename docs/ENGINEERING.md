# Engineering Practices — Coin Accounting

> **Binding.** These practices apply to every contributor, human or AI agent, from the first line of code. Changing them requires a PR that edits this file and gets human approval. A change that weakens a control also needs an ADR and an update to [`THREAT_MODEL.md`](THREAT_MODEL.md).

| | |
|---|---|
| Version | 0.1 (proposed, awaiting approval) |
| Last updated | 2026-09-27 |
| Related | [`PLAN.md`](../PLAN.md) · [`THREAT_MODEL.md`](THREAT_MODEL.md) · [`DEPENDENCIES.md`](DEPENDENCIES.md) · `docs/adr/` · `docs/architecture.md` |

Items marked **(verify at setup)** depend on tool behaviour to be confirmed when M0 configures the toolchain. If a tool doesn't behave as described, the M0 PR must propose an equivalent control here.

---

## 1. Principles

1. **Privacy and correctness before features.** The app handles data that can deanonymize the user and figures that go on tax returns. When in doubt, choose the conservative option and write an ADR.
2. **Few dependencies.** Every dependency is attack surface. In-process egress is an accepted risk (THREAT_MODEL R-4), so **supply-chain controls are the primary defence against data exfiltration**. Prefer the standard library, or a small, well-understood module we write ourselves.
3. **Evidence over assertion.** A feature is done when an end-to-end test shows it working, not when unit tests pass.
4. **Decisions are written down.** Significant choices go in ADRs; the architecture diagram and threat model stay in sync with the code.

## 2. Supply chain

Applies to **runtime and development dependencies alike**: linters, test tools and build tools execute code on the developer's machine too.

### 2.1 JavaScript (frontend)

| Control | Setting |
|---|---|
| Package manager | **pnpm**, with its version pinned via `packageManager` in `package.json`. npm/yarn are not used |
| Cooldown | `minimumReleaseAge: 10080` in `pnpm-workspace.yaml`: no package version younger than **7 days** can be installed. Exceptions (`minimumReleaseAgeExclude`) need a security-fix justification in the PR and in `DEPENDENCIES.md` |
| Install scripts | Lifecycle scripts (`preinstall`/`install`/`postinstall`) **disabled**: `onlyBuiltDependencies: []` and `strictDepBuilds: true`, so an unreviewed build script fails the install **(verify at setup)**. Adding a package to `onlyBuiltDependencies` requires an ADR |
| Lockfile | `pnpm-lock.yaml` committed; CI uses `pnpm install --frozen-lockfile` |
| Runtime assets | Everything bundled; no CDN, web fonts or remote resources (THREAT_MODEL T-106) |

### 2.2 Python (backend)

| Control | Setting |
|---|---|
| Package manager | **uv**, with its version pinned; Python version pinned in `.python-version` |
| Cooldown | `exclude-newer` in `pyproject.toml` `[tool.uv]` set to **now − 7 days**. If uv supports relative durations it is set directly; otherwise a checked-in script (`scripts/bump-exclude-newer`) rolls the timestamp forward, run only when intentionally updating dependencies **(verify at setup)** |
| Lockfile | `uv.lock` (with hashes) committed; CI and dev use `uv sync --locked` |
| No source builds | `no-build = true`: only wheels are installed, so no `setup.py` / build backend code runs at install time. An exception per package (`no-build-package`) needs an ADR and a manual review of the sdist |
| Indexes | PyPI only; no extra or alternative indexes |

### 2.3 Vetting a new dependency (all ecosystems)

Before a new direct dependency, or a new transitive one pulled in by an upgrade, is **installed or executed**:

1. **Justify it.** Why can't the standard library or ~100 lines of our own code do the job? Could a package we already use do it?
2. **Scan it with Socket.dev.** Installs are wrapped by **Socket Firewall** (`sfw pnpm install …`, `sfw uv sync …`), which blocks known-malicious packages before they reach disk **(verify at setup)**. The Socket report is reviewed for install scripts, network or filesystem access, obfuscated code, telemetry, new maintainers and typosquat signals. The Socket GitHub App (or `socket` CLI in CI) also reports on every PR that changes a lockfile.
3. **Check its health:** maintainers, release history, open security advisories, download base, transitive dependency count.
4. **Record it** in [`DEPENDENCIES.md`](DEPENDENCIES.md): name, ecosystem, runtime/dev, purpose, alternatives considered, whether it has network capability, Socket result, date, reviewer.
5. **Get human approval.** A PR that adds a dependency needs the human reviewer's explicit approval of that dependency. AI agents may propose dependencies, never add them unilaterally.

### 2.4 Updating dependencies

- Updates come in **deliberate batches** (at most monthly, plus urgent security fixes), never as a side effect of other work.
- Dependabot is configured with a **7-day cooldown** matching §2.1–2.2 **(verify at setup)**. Its PRs go through the same vetting as new dependencies: read the changelog, review the Socket diff.
- Security fixes may bypass the cooldown with a recorded justification.

### 2.5 CI and repository

- GitHub Actions are **pinned to full commit SHAs**, with a version comment. Only actions from GitHub (`actions/*`) or vetted publishers are allowed.
- Workflow `permissions: contents: read` by default; a job only gets more if it needs it. No `pull_request_target` workflows.
- CI needs **no secrets**.
- Branch protection on `main`:
  - PRs only
  - all required checks green
  - linear history not required: stacked PRs are merged with merge commits
  - Agent PRs are opened under the owner's GitHub account, and GitHub doesn't count an author's approval of their own PR. So "human approval" means **only the human merges or explicitly tells an agent to merge**, not GitHub's approval count. If a separate bot account is used for agents later, switch to a required approval
- **Signed commits** are required on `main` (THREAT_MODEL T-606).
- GitHub secret scanning and push protection are enabled.
- Test tooling downloads are verified:
  - `bitcoind` for regtest is checked against `SHA256SUMS` and the builder GPG signatures, with the version pinned
  - Playwright browser versions are pinned (THREAT_MODEL T-603)
- CI runs on **Linux and macOS** (PLAN: supported platforms).

## 3. Testing

### 3.1 Strategy: end-to-end first

| Layer | What it proves | Tooling |
|---|---|---|
| **E2E** (primary) | A user flow works: real regtest `bitcoind` + backend + browser | `pytest` harness driving `bitcoind -regtest` + **Playwright** |
| Integration | A component works against real neighbours (RPC client and scan jobs ↔ regtest node, cache ↔ DB, API ↔ DB) | `pytest` + regtest |
| Unit | Pure logic is correct at the edges (scan-result processing, doxx rules, tax engine, box selection) | `pytest`, Hypothesis for property tests and fuzzing |
| Frontend unit | Components with non-trivial logic | `vitest` |

- **Every user-visible feature ships with at least one E2E test** covering its main path. This is part of the Definition of Done (§8).
- **Mocks only at process boundaries we can't run.** The Bitcoin node is *not* mocked; tests use regtest. Mocking our own modules is not allowed in `tax/` and `doxx.py` tests.
- Tax scenarios use **hand-worked expected values derived from IRS rules**. Each test cites the rule, e.g. `# Treas. Reg. §1.1012-1(j)`, `# 2025 i8949 box I`.
- **Fixtures use public chain data or synthetic data only.** Never use the developer's own transactions or addresses as fixtures, because choosing them reveals ownership.

### 3.2 Coverage floors (CI-enforced)

| Scope | Floor | Measured by |
|---|---|---|
| Backend overall | **≥ 85 %** line + branch | `coverage.py`, combining unit, integration and E2E runs; the backend process is instrumented during E2E |
| `tax/`, `doxx.py`, `chain/` | **≥ 95 %** line + branch | same |
| Frontend | **≥ 70 %** lines (`vitest`), **plus** the E2E-per-feature rule | `vitest --coverage` |

Floors are minimums, not targets. Coverage cannot drop on `main`, and a PR that lowers coverage in a floored module fails CI.

### 3.3 Mutation testing

`mutmut` runs on `tax/` and `doxx.py`:
- weekly on a schedule
- before each milestone is closed

The **surviving-mutant budget is 0** for `tax/engine.py` and the box-selection rules, and ≤ 5 % elsewhere in those modules. Each accepted survivor is listed with a reason (e.g. an equivalent mutant).

### 3.4 Anti-test-slop rules

A test exists to fail when behaviour breaks. Reviewers (human and AI) reject tests that:

1. **Assert nothing meaningful:** `assert result`, `assert x is not None`, or `assert True` alone; asserting only that a function was called.
2. **Mirror the implementation:** expected values computed by the same logic under test.
3. **Mock the thing under test,** or mock our own modules in `tax/`/`doxx.py` tests.
4. **Snapshot everything:** whole-report snapshots are allowed only as hand-reviewed golden files, with a README explaining how each value was derived.
5. **Were changed to match new output** without an explanation in the PR of why the old expectation was wrong.
6. **Depend on timing or order:** `sleep`-based waits, wall-clock dates, test order, or network access (the socket guard, §5.4, fails these).
7. **Duplicate another test** without adding a distinct case.
8. **Have unclear names.** Names must state the behaviour and, where relevant, the threat or rule ID, e.g. `test_late_identification_falls_back_to_fifo__T508`.

**Test audits:** at each milestone close, and at least monthly while coding is active, a test-audit pass reviews the suite against these rules plus the mutation report. It deletes or strengthens weak tests, and its findings go into the milestone PR. An AI reviewer may do a first pass; a human approves the result.

## 4. Design records

### 4.1 ADRs

- **Location/format:** `docs/adr/NNNN-kebab-title.md`, [MADR](https://adr.github.io/madr/) template. Status: `proposed → accepted`, later `superseded by NNNN` or `deprecated`.
- **ADRs are never rewritten** after acceptance. To change a decision, write a new ADR that supersedes the old one.
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
- A CI check compares the file's hash with the hash recorded in the most recent accepted ADR that touched it. **Changing the diagram without an accepted ADR fails CI.**
- Code that contradicts the diagram is a bug. Either fix the code, or change the diagram through an ADR.

### 4.3 Threat model

- Any PR touching a boundary, asset, store, network flow, dependency or tax rule updates `THREAT_MODEL.md` in the same PR: statuses, evidence links, changelog.
- A threat moves to **Verified** only when a linked test would fail if the mitigation were removed.

## 5. Code standards

### 5.1 Languages and tooling

| Area | Standard |
|---|---|
| Python | 3.12; `ruff` (lint + format); `mypy --strict`; no `# type: ignore` without a reason comment |
| TypeScript | `strict: true`; ESLint (incl. React rules); no `any` without a reason comment |
| SQL | Parameterized queries only; schema changes via migrations |
| Formatting | Enforced in CI; not debated in review |

### 5.2 Rules enforced by lint or custom checks

| Rule | Why | Enforcement |
|---|---|---|
| Network imports (`socket`, `http.client`, `urllib.request`, `httpx`, …) only in `rpc.py` and `prices/` | T-305 | ruff `banned-api` / per-file allowlist |
| No `float` in `tax/`; money is `Decimal`, BTC amounts are integer sats | T-502 | custom AST check in CI |
| No `eval`/`exec`, `pickle`, `shell=True`, or `subprocess` outside an allowlisted launcher module | Code execution | ruff (`S` rules) + banned-api |
| No `dangerouslySetInnerHTML`, no inline `style` props, no external URLs in source or bundle | T-104, T-106 | ESLint rules + bundle scan |
| Timestamps are timezone-aware UTC; conversion to local time only for display and for tax dates | Tax dates (PLAN §7) | lint + review |
| Logging only through the redacting logger; never `print` user data | T-403 | ruff (`T20`) + review |
| No secrets, real addresses, txids of the developer, or real user data in code, fixtures, issues or PRs | T-607 | review + secret scanning |

### 5.3 Error handling

- **Fail closed** on anything touching integrity: chain-data fetching and caching, reorgs, storage checks, price imports.
- Never swallow exceptions silently. User-facing errors must not echo sensitive values.

## 6. AI coding agents

Agents (Claude Code, Codex and others) follow `AGENTS.md`, which makes this document and the threat model binding. Their instructions live in `AGENTS.md`; `CLAUDE.md` only imports it. In addition:

- **No real data.** Agents never run while a VeraCrypt volume with real data is mounted, and never get access to real DBs, logs, exports or configs. Development and E2E use regtest and synthetic data only. Mainnet smoke tests are run by the human (THREAT_MODEL T-607).
- **Scope.** Agents don't:
  - add dependencies without the §2.3 vetting and human approval
  - weaken a control
  - change `docs/architecture.md` without an ADR
  - commit to `main` directly
- **Transparency.** Agent-authored commits carry a `Co-Authored-By` trailer. PR descriptions state what was verified (commands run, tests added) and what wasn't.
- **Review.** Every agent PR gets human review. Independent AI reviews (e.g. a second model) are encouraged for design docs and security-relevant code. Their findings are verified before being acted on, not applied blindly.

## 7. Workflow

- **Branches:** `main` is always releasable. Work happens on short-lived branches (`<type>/<topic>`, e.g. `feat/scan-jobs`, `docs/adr-0003`).
- **PRs:**
  - small and focused
  - opened as **drafts** for human review on GitHub
  - stacked PRs are allowed and merged with merge commits
  - each PR description lists the affected threat IDs and ADRs
- **Commits:** imperative subject ≤ 72 chars; body explains *why*; signed.
- **Docs travel with code:** PLAN, threat model, ADRs, diagram and `DEPENDENCIES.md` are updated in the same PR as the change that affects them.

## 8. Definition of Done

A change is done only when:

- [ ] Behaviour is covered by an **E2E test** (for user-visible features) and by unit/integration tests where logic warrants them
- [ ] Coverage floors and the mutation budget hold; CI is green on Linux and macOS
- [ ] Tests comply with §3.4, with no slop
- [ ] `THREAT_MODEL.md` statuses and evidence links are updated (or explicitly "no change")
- [ ] ADR written or updated if §4.1 applies; the architecture diagram still matches
- [ ] Any new dependency is vetted and recorded in `DEPENDENCIES.md`, with human approval
- [ ] No new network flow outside THREAT_MODEL §6; the socket guard stays green
- [ ] User-facing docs updated where behaviour changed
- [ ] Human approval on the PR

## 9. Enforcement summary

| Practice | Enforced by |
|---|---|
| Cooldowns, frozen lockfiles, no install scripts, wheels-only | Tool config + CI check that the config is present |
| Socket scanning | `sfw` wrapper locally and in CI + Socket PR report |
| SHA-pinned actions, minimal permissions | CI lint of workflow files |
| Coverage floors | CI (`coverage.py`, `vitest`) |
| Mutation budget | Scheduled CI job + milestone checklist |
| Banned APIs, float ban, CSP/XSS rules | ruff / custom AST check / ESLint / bundle scan |
| Diagram ↔ ADR hash | CI check |
| Threat model / ADR / DEPENDENCIES updates | PR template checklist + human review |
| Test-slop rules | Review checklist + periodic test audit |
| No real data for agents | `AGENTS.md` + human discipline (Documented, T-607) |

## 10. Changelog

| Date | Version | Change |
|---|---|---|
| 2026-09-27 | 0.1 | Initial proposal (P0.2) |
