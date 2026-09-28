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

## Decision Outcome

Option 2 (user decision, 2026-09-28), as specified in `.claude/skills/review-panel/SKILL.md`.
- **Running `/review-panel #N` is the human's explicit instruction to merge PR #N** once a round of four reviews has no valid P1/High/Critical findings, CI is green, and there are **no human decision points**. This extends the procedural merge rule of [ADR 0018](0018-repository-governance.md).
- **Human decision points always stop the merge** and go to the human in plain language: new or changed dependencies and toolchain pins (ENGINEERING §2.4), ADR or architecture changes, weakened controls or new accepted risks, tax positions, privacy trade-offs, scope changes, and high-stakes rejected findings.
- The agent may open GitHub issues (label `review-panel`) for valid non-P1 findings, and post review and triage comments.
- A loop guard stops the panel for human input after 5 rounds.
- Only session tokens are used. When a limit is hit, the panel pauses and resumes from its state file.

### Consequences

- Good: PRs progress without waiting for the human, and every review, triage and decision is on the PR.
- Bad: an agent merges with the owner's credentials. Enforcement stays procedural (THREAT_MODEL T-605). A validation mistake by the agent could merge a real defect; four independent reviews per round, and the human decision points, limit this.

## References

- `.claude/skills/review-panel/SKILL.md`; AGENTS.md; ADR 0018; ENGINEERING §6; THREAT_MODEL T-605
