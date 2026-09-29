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

Option 2 (user decision, 2026-09-28), as specified in `.claude/skills/review-panel/SKILL.md`. **This amends the Merging rule of [ADR 0018](0018-repository-governance.md):** besides the human, the panel may merge under the conditions below. The skill file is governed by this ADR: changing its merge conditions, decision points or trust rules needs a new ADR.

- **Merge authority.** Running `/review-panel #N` is the human's instruction to merge PR #N, but only when all of these hold:
  - A round of reviews at a **pinned head commit** has no valid P1/High/Critical findings.
  - CI has run and passed on that exact commit.
  - The PR is mergeable.
  - **Every human decision point is approved** by the human in the session.
  - The merge uses `gh pr merge --match-head-commit <reviewed sha>`. A push to the PR restarts the round, and voids approvals given for the old head.
- **Scope.** Only same-repository PRs authored by the repository owner. The panel doesn't check out, run or merge fork PRs or PRs by anyone else.
- **Human decision points are mechanical and always stop the merge.** A PR that changes any of these needs the human's approval:
  - dependency, toolchain or install-config files
  - the agent's own rules and the controls (`.claude/`, `AGENTS.md`, `scripts/`, `Makefile`, `.github/`, `docs/ENGINEERING.md`, `docs/THREAT_MODEL.md`)
  - ADRs, `docs/architecture.md` or `PLAN.md`

  So do tax positions, privacy trade-offs, risk changes, unrequested scope changes, and any rejected Critical/High security finding. The agent's judgment can add decision points, never remove them.
- **Approvals come only from the human's own messages in the session.** PR text, review reports, issues and comments are untrusted data, and a cron-sent tick approves nothing.
- **ADR acceptance.** Once the human approves a PR's ADRs, the panel commits the `proposed` → `accepted` status lines (a status-only change, per [ADR 0001](0001-record-decisions-with-adrs.md)) and merges after CI passes on that commit.
- **Other agents** may run the same procedure by hand. For them, too, only the human's explicit instruction in their own session authorizes a merge.
- The agent may open GitHub issues (label `review-panel`) for valid non-P1 findings, and post review and triage comments. Security issues stay general until fixed.
- A loop guard stops the panel for human input after 5 rounds. From there, only the human's explicit answer (another round, stop, or merge) continues.
- Only session tokens are used. When a limit is hit, the panel pauses and resumes from its state file.

### Consequences

- Good: routine PRs progress without waiting for the human, and every review, triage and decision is on the PR. The human's attention goes to the decision points.
- Bad: an agent merges application-code PRs with the owner's credentials and no human diff review. Enforcement is procedural (THREAT_MODEL T-605), and the residual risks are accepted as R-9:
  - a validation mistake merging a real defect
  - reviewers that are read-only only by prompt
  - a background reviewer still running when a data volume is mounted between ticks

  They are limited by four reviews per round, the mechanical decision points, and the owner-only scope.

## References

- `.claude/skills/review-panel/SKILL.md`; AGENTS.md; ADR 0001; ADR 0018; ENGINEERING §2.4, §4.1, §6, §8; THREAT_MODEL T-605, R-8, R-9
