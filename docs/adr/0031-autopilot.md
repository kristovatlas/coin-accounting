---
status: accepted
date: 2026-10-05
deciders: repository owner (human), drafted by Claude Code
amends: 0018, 0020, 0030
---

# 0031: Autopilot: agents merge everything except human decisions

## Context and Problem Statement

Cruise mode (ADR 0030) made reviews lighter and let a mechanical gate merge PRs, but the gate refused every PR that touched a security-critical module, the `tax/`, `doxx/` or `chain/` engines, a binding document or tool configuration. In practice almost every M0.3 PR touched one of these, so every PR still waited for the owner to merge it, often after the review panel had already cleared it. The owner (2026-10-05) wants development to stop waiting on them for work that is already planned and reviewed: "no more waiting on me to merge PRs because they touch certain parts of the code". The owner also wants the threat model to focus on protecting the app once it's built, rather than on gating the development process.

## Considered Options

1. Keep cruise mode as it is (ADR 0030).
2. **Autopilot:** agents start the review panel themselves, and merge every reviewed PR through the gate, except where a human decision is genuinely needed, or where the change would loosen the agents' own controls.
3. Remove the gate and let agents merge with `gh pr merge` after a clean panel.

## Decision Outcome

Option 2 (owner decision, 2026-10-05), named **autopilot**. It runs under `PROCESS_MODE` = `cruise`; switching to `standard` still restores ADR 0018, 0020 and 0023 unchanged.

### What changes

1. **Agents start the review panel** on every PR they open. The owner no longer types `/review-panel`.
2. **Every agent PR the gate can merge gets the cruise review profile** (ADR 0030 §1): two reviewers, or four for risky paths; at most two rounds; P1 means only a confirmed Critical/High finding, a broken or flaky test, a real leak, or wrong tax figures. An agent PR that touches a path in §3, which goes to the owner anyway, gets the standard profile, which is stricter; the panel decides this from the gate's own list (`cruise_merge.py --list-blocked`). "Agent PR" means one an agent recorded when it opened it (in the run's state, or the panel's `agent-prs.txt`); any other PR, even the owner's ready one, gets the standard profile. A PR the human starts the panel on also gets the standard profile.
3. **The gate merges any reviewed PR from any branch of this repository**, except changes to these paths, which stay with the owner. The list keeps the agents from changing their own controls **through the files that configure them**; in-file suppressions (`# mypy: ignore-errors`, `@ts-nocheck`), copied-in code and similar changes inside ordinary files rest on the review, where the Opus tripwire looks for them and sends any it finds to the owner (R-12):
   - **Human decisions:**
     - dependency manifests, lockfiles and install configuration, in any directory: `pyproject.toml`, `uv.lock`, `uv.toml`, `package.json`, `pnpm-lock.yaml`, `pnpm-lock.times.json`, `pnpm-workspace.yaml`, `.npmrc`, `.pnpmfile.*`, `.python-version`, `.node-version`, `requirements*.txt`, `constraints*.txt` (ENGINEERING §2.4: the owner approves the Socket verdict). A gate test keeps this list in step with the Makefile's approval targets, which treat whatever is on `main` as approved.
     - ADRs (`docs/adr/`) and the architecture baseline (`docs/architecture.md`, hash-locked by ADR 0014)
   - **The agents' own controls,** so an agent can never loosen its own checks:
     - `scripts/` (the gate, the guard hook, the repository and lockfile checks, the toolchain lock)
     - `.github/` (CI)
     - agent instructions, skills and tool configuration **in any directory**: `.claude/`, `.codex/` and similar tool directories, any `*agents*.md` or `*claude*.md` (including `AGENTS.override.md` and scoped files such as `backend/AGENTS.md`), any `SKILL.md`, `.mcp.json`
     - the `Makefile` (the install and approval targets)
     - `PROCESS_MODE`, `docs/cruise-mode.md`
     - the test socket guard and what switches it on: `backend/tests/socket_guard.py`, `backend/tests/__init__.py`, every `conftest.py`, and a `pytest.toml`, `.pytest.toml`, `pytest.ini`, `tox.ini` or `setup.cfg`, which pytest would read before `pyproject.toml`
     - the settings of CI's other checks, wherever a file can override `pyproject.toml` for a subtree: `ruff.toml`, `.ruff.toml`, `mypy.ini`, `.coveragerc`, and the frontend's `eslint.config.*`, `.eslintrc*` and `vitest.config.*`
     - other agents' and editors' configuration: `*gemini*.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `.aider*`, `.devcontainer.json`, and the `.vscode/`, `.idea/`, `.windsurf/`, `.devcontainer/` and similar directories (which can hold MCP servers, tasks and lifecycle commands that run on open)
     - `vite.config.*` (Vitest reads its coverage settings there when there's no `vitest.config.*`) and `mutation-exclusions.md`
     - Socket's repository config (`socket.yml`/`socket.yaml`), which could ignore paths or turn off the PR alerts dependency approvals rely on
     - anything new at the top of the repository other than under `backend/`, `frontend/`, `e2e/` and `docs/` (and `PLAN.md`, `README.md`), and any Python module directly under `backend/` or `e2e/` outside their existing packages: CI runs `python -m coverage`/`pytest`/`unittest` from the root, so such a file could stand in for a test tool and turn the tests off
   - **Supply chain and the coverage floor:** vendored or minified third-party code (`vendor/`, `third_party/`, `node_modules/`, `site-packages/`, `*.min.*`), and deleting or renaming a file in the `tax/`, `doxx/` or `chain/` engines, whose directory names key the 95 % floor and the mutation runs
4. **Everything else merges automatically**, including the security-critical modules (`api/`, the launcher, `storage/`, `rpc.py`, `config.py`), the `tax/`, `doxx/` and `chain/` engines with their tests and golden files, the frontend (SVG files included), the E2E specs, build configuration such as `tsconfig.json` and `playwright.config.*` (a `strict` flag or a test filter in them is judged by the review, R-12), and the living binding documents (`THREAT_MODEL.md`, `ENGINEERING.md`, `PLAN.md`, `DEPENDENCIES.md`). A change to a living binding document that **weakens a control** still needs an ADR and the owner (AGENTS.md). The Opus tripwire looks for one, and any such flag, whatever its rating, sends the PR to the owner; a reviewer finding of one is a human item. The gate itself can't tell, so this rests on the review (R-12).
5. **The gate requires the panel's clearance.** The review panel sets a commit status `review-panel` = `success` on the exact SHA only after a clean round (or the cruise profile's post-round-2 fixes) and an Opus tripwire with no flag of Medium or above. When the branch is refreshed with `main`, the panel sets it again on the new head, only after CI and both tripwires pass there. Once any round has found a Critical security issue, a committed secret or real user data, it never sets it again, and it labels the PR `autopilot-blocked`, which the gate refuses; only the owner removes that label. The gate accepts the status only when the owner's account set it. The gate refuses a commit without it. The owner's token can set that status too, so it doesn't stop a determined agent (R-9), but it stops a merge that no panel reviewed, from any branch.
6. **The mechanical tripwire becomes informational** for its path, content, removed-line and deleted-file flags, which are posted for the record. Every other kind blocks: symlinks, submodules, executable bits, changes it can't parse, and any kind added later until it is classified on purpose (a gate test lists them). The gate's own dynamic-code scan is removed; the **Opus tripwire** reviews the full change, and any flag it rates Medium or above sends the PR to the owner as a draft.
7. **The milestone-closing PR** of a `/cruise` run (THREAT_MODEL statuses, PLAN progress) merges through the gate like any other PR, with the cruise profile, unless it touches a path in §3.
8. **PRs outside a `/cruise` run** follow the same path: the review panel runs the gate itself at its hand-off. Every merge the gate makes, in a run or not, is listed in a tracking issue: the run's, or the standing **"Autopilot merges"** issue.
9. **Still the owner's, whatever the path:**
   - a Critical security finding, a committed secret or real user data (ADR 0030)
   - a question at the level of an ADR (ENGINEERING §4.1)
   - approving dependencies, which agents never do (AGENTS.md, unchanged)
   - anything that touches real data or mainnet (AGENTS.md, unchanged)

### What doesn't change

- The gate's other conditions: the owner's PR at the exact reviewed SHA, containing the current `main`; CI green (`checks` and `tests` on Linux and macOS); classic branch protection; the merge token; the stop file. Only text files of the listed types (this ADR adds `.svg`, served under the frontend's CSP, and `.txt`; the `.txt` files that matter to installs, `requirements*.txt` and `constraints*.txt`, stay blocked).
- Merging only through `main`'s copy of the gate, never `gh pr merge`.
- The tests, lint, `make check` and the coverage floors, all enforced by CI on every PR.

### Consequences

- Good: planned work lands without waiting on the owner; the owner's attention goes only to dependencies, ADR-level decisions, Critical findings, and changes to the agents' own controls.
- Bad: **code in the security-critical modules and the tax engine now merges with AI review only.** A defect the reviewers and CI miss reaches `main` without a human look. That is accepted (THREAT_MODEL R-12). The owner sees every merge in a tracking issue (§8), can revert any of them, and can switch back to `standard` at any time.
- Neutral: the threat model's development-process threats (T-605, R-9, R-11) stay recorded, narrowed by R-12, but the threat model's focus is the shipped app.
