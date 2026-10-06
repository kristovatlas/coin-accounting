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
2. **Every agent PR gets the cruise review profile** (ADR 0030 §1): two reviewers, or four for risky paths; at most two rounds; P1 means only a confirmed Critical/High finding, a broken or flaky test, a real leak, or wrong tax figures.
3. **The gate merges any reviewed PR from any branch of this repository**, except changes to these paths, which stay with the owner:
   - **Human decisions:**
     - dependency manifests and lockfiles: `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`, `pnpm-lock.times.json`, `pnpm-workspace.yaml`, `.npmrc`, `.pnpmfile.*` (ENGINEERING §2.4: the owner approves the Socket verdict)
     - ADRs (`docs/adr/`) and the architecture baseline (`docs/architecture.md`, hash-locked by ADR 0014)
   - **The agents' own controls,** so an agent can never loosen its own checks:
     - `scripts/` (the gate, the guard hook, the repository and lockfile checks, the toolchain lock)
     - `.github/` (CI)
     - `.claude/` (skills and settings), `AGENTS.md`, `CLAUDE.md`, `.mcp.json`
     - the `Makefile` (the install and approval targets)
     - `PROCESS_MODE`, `docs/cruise-mode.md`
     - the test socket guard
4. **Everything else merges automatically**, including the security-critical modules (`api/`, the launcher, `storage/`, `rpc.py`, `config.py`), the `tax/`, `doxx/` and `chain/` engines with their tests and golden files, the frontend, the E2E specs, tool configuration such as `conftest.py` or `vite.config.*`, and the living binding documents (`THREAT_MODEL.md`, `ENGINEERING.md`, `PLAN.md`, `DEPENDENCIES.md`).
5. **The mechanical tripwire becomes informational.** Only structural flags block: symlinks, submodules, executable bits and changes it can't parse. Every other flag (paths, content such as "network use", "test weakening" or "dynamic code", removed lines) is posted for the record. The **Opus tripwire** still reviews the full change, and any flag it rates Medium or above sends the PR to the owner as a draft.
6. **Still the owner's, whatever the path:**
   - a Critical security finding, a committed secret or real user data (ADR 0030)
   - a question at the level of an ADR (ENGINEERING §4.1)
   - approving dependencies, which agents never do (AGENTS.md, unchanged)
   - anything that touches real data or mainnet (AGENTS.md, unchanged)

### What doesn't change

- The gate's other conditions: the owner's PR at the exact reviewed SHA, containing the current `main`; CI green (`checks` and `tests` on Linux and macOS); classic branch protection; the merge token; the stop file.
- Merging only through `main`'s copy of the gate, never `gh pr merge`.
- The tests, lint, `make check` and the coverage floors, all enforced by CI on every PR.

### Consequences

- Good: planned work lands without waiting on the owner; the owner's attention goes only to dependencies, ADR-level decisions, Critical findings, and changes to the agents' own controls.
- Bad: **code in the security-critical modules and the tax engine now merges with AI review only.** A defect the reviewers and CI miss reaches `main` without a human look. That is accepted (THREAT_MODEL R-12). The owner sees every merge in the run's tracking issue, can revert any of them, and can switch back to `standard` at any time.
- Neutral: the threat model's development-process threats (T-605, R-9, R-11) stay recorded, but the threat model's focus is the shipped app.
