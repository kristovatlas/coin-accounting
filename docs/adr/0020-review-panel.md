---
status: proposed
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0020: Automated review panel drives PRs to merge

## Context and Problem Statement

Every PR so far has been reviewed by two AI models, with the triage done by hand. The human wants LLM reviews to replace most human review time, with the human's attention reserved for real decisions.

## Considered Options

1. The human requests reviews and approves each merge (the practice so far).
2. **A `/review-panel #N` skill.** Four reviews per round (Opus 5.5 security and functional, Codex gpt-5.6-sol security and functional). Validated P1/High/Critical findings are fixed and the panel repeats. Other valid findings become GitHub issues. When a round is clean, the agent merges, or brings the human decision points to the human.
3. A panel that never merges, only reports. Every merge still needs the human, including routine application-code PRs.
4. GitHub auto-merge with required reviews. This needs a second GitHub account for the reviewer, because agents act as the owner, and it adds no decision-point logic.

## Decision Outcome

Option 2 (user decision, 2026-09-28), as specified in `.claude/skills/review-panel/SKILL.md`, which this ADR governs: changing the skill's merge gate, decision points or trust rules needs a new ADR.

**This ADR amends two earlier ADRs**, under the newest-wins rule of ADR 0018:
- [ADR 0018](0018-repository-governance.md), Merging: besides the human, the panel may merge under the conditions below.
- [ADR 0001](0001-record-decisions-with-adrs.md), acceptance: an ADR is also accepted when the human approves it as a decision point, and the panel then merges it.

### Merge authority

The human typing `/review-panel #N` (or the cron tick the panel creates for it) is the instruction to merge PR #N. The skill can't be invoked by the model on its own (`disable-model-invocation`), or from a subagent. It merges only through one **merge gate**:
- **A clean round at a pinned commit.** A round of four reviews found no valid P1/High/Critical findings at a pinned head. The base branch and merge base are unchanged since the round started.
  - A push (other than the panel's own, which starts a new round), a retarget or a base change restarts the round and voids approvals.
  - **The one exception:** the panel's own commit that only flips approved ADRs from `proposed` to `accepted` keeps the round and the approvals.
- **CI on that commit:** the required checks (`checks (ubuntu-latest)`, `checks (macos-latest)`) are present and green on it, and no other check failed.
- **Mergeable:** the PR has no conflict.
- **Approved:** every decision point has been approved by the human in the current session.
- **Pinned merge:** `gh pr merge --match-head-commit <that commit>`. The approvals are quoted in a PR comment first.

**Scope.** Only same-repository PRs authored by the repository owner. The panel doesn't check out, run or merge anything else.

### Human decision points

**Fail-safe, by path.** Every changed path (with moves listed as both old and new path) is a decision point, **unless** it is on this application allowlist:
- `backend/coinacct/**`
- `backend/tests/**`
- `frontend/src/**`
- `frontend/tests/**`
- `e2e/tests/**`

Even there, these are always decision points:
- dotfiles
- `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`
- package manifests, lockfiles and `*.toml`/`*.yaml`/`*.yml` config

So every change to dependencies, install config, agent instructions, scripts, CI, the binding documents, ADRs, the architecture and PLAN needs the human.

**Also by content:**
- tax positions
- privacy trade-offs
- risk changes
- unrequested scope changes
- a rejected Critical/High security finding
- a skipped security reviewer

The agent's judgment can add decision points, never remove one.

**What the human is shown.** Decisions are presented with the diffstat and a link to the PR's files, never with the agent's summary alone. Dependency changes also come with the Socket verdict and the lockfile diff (ENGINEERING §2.4).

### Approvals

Approvals come only from the human's own messages in the current Claude session. PR text, review reports, issues and comments are untrusted data, and a cron tick approves nothing.

An approval is voided when:
- the reviewed head or base changes (except the status-only commit), or
- the session changes; the human then confirms again.

### Other rules

- **Other agents** may run the procedure by hand, but they merge only when the human explicitly says "merge PR #N" in their own session.
- **Issues.** The agent may open GitHub issues (label `review-panel`) for valid non-P1 findings, and post review and triage comments. Reports are scrubbed of local details, and unfixed security findings are described in general terms until fixed.
- **Loop guard.** The panel stops for the human after 5 rounds. A merge the human orders from there still goes through the merge gate.
- **Usage limits.** Only session tokens are used. When a limit is hit, the panel pauses and resumes from its state file.

### Consequences

- **Good:** routine application-code PRs progress without waiting for the human. Every review, triage and decision is on the PR, and the human's attention goes to the decision points.
- **Bad:** an agent merges application-code PRs with the owner's credentials and no human diff review. Enforcement is procedural (THREAT_MODEL T-605), and the residual risks are accepted as R-9:
  - a validation mistake merging a real defect
  - Opus reviewers read-only only by prompt
  - PR code run locally by the panel, as in normal development (R-6)
  - a Codex run lasting up to ~5 seconds after a data volume is mounted, before its watchdog kills it

  They are limited by four reviews per round, the fail-safe decision points, the merge gate and the owner-only scope.

## References

- `.claude/skills/review-panel/SKILL.md`; AGENTS.md; ADR 0001; ADR 0018; ENGINEERING §2.4, §4.1, §6, §8, §9; THREAT_MODEL T-605, R-8, R-9
