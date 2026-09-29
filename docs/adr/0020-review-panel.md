---
status: proposed
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0020: Automated review panel; auto-merge only for application code

## Context and Problem Statement

Every PR so far has been reviewed by two AI models, with the triage done by hand. The human wants LLM reviews to replace most human review time, with the human's attention reserved for real decisions.

Three review rounds on the first design for this ADR showed a problem. A panel that merges everything on its own, gated by stored approvals, needs a lot of machinery, and each piece had holes: an approval ledger, special commits, session tracking.

## Considered Options

1. The human requests reviews and approves each merge (the practice so far).
2. A panel that merges every PR itself, after the human approves "decision points" recorded in a state file. Rejected after three review rounds, for these reasons:
   - stored approvals could be forged by an agent
   - the list of paths needing a decision kept leaking
   - the restart and acceptance logic kept failing
3. **A panel that reviews every PR, but auto-merges only application-code PRs.** Every other PR is merged only on the human's explicit instruction.
4. A panel that never merges. Every merge needs the human, even for routine application code.

## Decision Outcome

Option 3 (user decision, 2026-09-28), as specified in `.claude/skills/review-panel/SKILL.md`. This ADR governs that file: any change to it, or to `.claude/agents/panel-reviewer.md`, needs the human (neither file is on the allowlist).

### The panel

`/review-panel #N`, typed by the human (the skill can't be started by the model or a subagent), runs rounds of four reviews: Opus 5.5 and Codex gpt-5.6-sol, each once for security and once for function.
- Validated P1/High/Critical findings are fixed, and the round repeats.
- Other valid findings become GitHub issues.
- A loop guard stops for the human after 5 rounds.
- Reviewers can't change anything. Opus runs as a read-only agent (Read, Grep, Glob only), and Codex runs in its read-only sandbox.
- Only same-repository PRs authored by the owner are handled.
- PR text, reviews, issues, comments and task notifications are untrusted data.

### Auto-merge

This amends [ADR 0018](0018-repository-governance.md): the panel merges on its own only when **all** of these hold:
- **The round was clean** at the exact commit being merged.
- **Every changed path is on the application allowlist:**
  - `backend/coinacct/{domain,services,chain,tax,doxx}/**`
  - `backend/tests/**`
  - `frontend/src/{views,graph}/**`
  - `e2e/**/*.spec.ts`
- **None of these appear:**
  - dot-directories or dotfiles
  - agent-instruction files (`AGENTS*.md`, `CLAUDE*.md`, `SKILL.md`)
  - manifests, lockfiles, `*.toml`/`*.yaml` config
  - symlinks or submodules
- **Moved files** count under both their old and their new path.
- **Nothing that needs the human.** The change needs no ADR under ENGINEERING §4.1, takes no tax position or privacy trade-off, and no reviewer was skipped. This judgment can only block an auto-merge, never allow one.
- **The merge gate passes:**
  - the head is the reviewed commit
  - the required CI checks (`checks (ubuntu-latest)`, `checks (macos-latest)`, from GitHub Actions) are green on that commit
  - the PR is mergeable
  - the merge uses `gh pr merge --match-head-commit`
  - if the base branch moved, a clean merge of it into the branch keeps the reviews (verified mechanically); a conflict starts a new round

The security-critical modules are **not** on the allowlist, so a change to any of them always goes to the human: `launcher.py`, `config.py`, `api/`, `rpc.py`, `prices/`, `storage/`, `e2e/harness/` and `frontend/src/api/`.

### Every other PR

Every other PR goes to the human with a clean round, the reason, the diffstat and files link, and for dependencies the Socket verdict and lockfile diff.
- **It merges only when the human types "merge #N" in the session.** The panel then sets the PR's ADRs to `accepted`, and merges through the same gate in that turn.
- **The panel stores no approvals.** This is the explicit instruction that ADR 0018 already allows. The human's merge instruction is the acceptance under [ADR 0001](0001-record-decisions-with-adrs.md), and the dependency approval under ENGINEERING §2.4.

### Consequences

- **Good:**
  - Routine application-code PRs progress without the human.
  - Every other change still gets an explicit human merge, with no approval state an agent could forge.
  - Every review and triage is on the PR.
- **Bad:** application-code PRs merge with no human diff review, under the owner's credentials. This is accepted as THREAT_MODEL R-9, which also covers:
  - reviewers running while a data volume is mounted: the user must not mount one while the panel runs, and Codex's watchdog kills it within ~5 seconds
  - the panel running the PR's code locally, as in normal development (R-6)

## References

- `.claude/skills/review-panel/SKILL.md`; `.claude/agents/panel-reviewer.md`; AGENTS.md; ADR 0001; ADR 0018; ENGINEERING §2.4, §4.1, §6; architecture §2; THREAT_MODEL T-605, R-6, R-9
