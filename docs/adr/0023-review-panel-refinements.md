---
status: accepted
date: 2026-09-29
deciders: repository owner (human), drafted by Claude Code
amends: 0020
---

# 0023: Review panel refinements: severity rules, round-limit walkthrough, no data-volume checks, no symlinks

## Context and Problem Statement

PR #8 (ADR 0020) went through six review rounds. Most findings in the later rounds fell into two groups. Some were process edge cases where the worst outcome was that the panel stalled or asked the human. Others were variations on "an AI could be steered", which only real sandboxing could close. The human made four decisions during that review and deferred them to this follow-up (issue #54).

## Decision Outcome

User decisions, 2026-09-29. They amend [ADR 0020](0020-review-panel.md) and `.claude/skills/review-panel/SKILL.md`.

1. **Severity rules.** When validating findings, the panel downgrades these to non-P1, whatever severity the reviewer gave. They become issues, or are covered by R-9.
   - **Fail-safe process edge cases:** a gap whose worst outcome is that the panel stalls, repeats work or asks the human.
   - **Unsandboxed-AI findings:** "an AI could be prompt-injected or steered", or "this rule is only an instruction". AI agents are **not sandboxed**, since that would be a very large engineering programme; the risk is accepted in R-9.
2. **Round-limit walkthrough.** After round 5, if the latest round still has validated P1s, the panel doesn't just ask "another round or stop". It presents each P1 in plain language, with its own view of the severity, and the human decides per item:
   - **downgrade:** filed as an issue
   - **keep:** fixed, with one more round
   - **accept:** a known risk, listed in the hand-off

   If nothing is kept, the round counts as clean and the PR goes to the hand-off.
3. **No data-volume checks in the panel.** The owner is the lone developer. They never keep real data, or mount VeraCrypt, on the machine where agents and reviewers run.
   - The panel drops its per-tick VeraCrypt check, the Codex watchdog and the "don't mount while the panel runs" rule.
   - The general `AGENTS.md` check and the guard hook (T-607) are unchanged for other agents.
4. **No symbolic links or git submodules in the repository.**
   - A symlink can point anywhere on the machine that checks it out, and a submodule pulls in code no review here covers. The project needs neither.
   - `scripts/check_repo_files.py` fails CI on any tracked mode `120000`/`160000` or a `.gitmodules` file.
   - The panel refuses such PRs outright; the "review anyway" override is removed.
   - Lifting the ban needs a new ADR.

### Consequences

- **Good:**
  - Fewer review rounds spent on findings that can't lead to an unsafe merge.
  - The human settles stubborn findings in one plain-language conversation.
  - The panel has less machinery.
  - A whole class of path tricks is closed repository-wide.
- **Bad:**
  - Some real but fail-safe bugs are deferred to issues.
  - The owner-only checks and the no-data-on-the-dev-machine practice rest on the owner's habits (R-9).

## References

- ADR 0020; `.claude/skills/review-panel/SKILL.md`; `scripts/check_repo_files.py`; THREAT_MODEL R-6, R-9, T-607; issues #54, #55, #56
