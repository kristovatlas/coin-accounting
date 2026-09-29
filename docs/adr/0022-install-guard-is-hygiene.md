---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0022: The install-command guard is hygiene against accidental installs

## Context and Problem Statement

`scripts/agent_guard.py` (a Claude Code hook) and `scripts/banned_commands.py` (also used by CI) block install and fetch-and-run commands that bypass the `make` targets and Socket Firewall. Three review rounds on PR #7 kept finding new ways past them: wrappers, exported variables, `eval`, commands written into a script file, and so on. A command-text matcher can't win against a determined agent, because a shell can express the same action in unlimited ways.

## Considered Options

1. **Hygiene only:** the guard catches accidental or habitual install commands. Deliberate evasion is an accepted risk.
2. Hygiene, plus a sandbox network allowlist as a real boundary (evaluated later).
3. Keep hardening the guard against deliberate evasion.

## Decision Outcome

Option 1 (user decision, 2026-09-28).
- The guard's job is to stop the install commands an agent would type **out of habit or by mistake**: bare `npm`/`pnpm`/`uv`/`pip` installs, `npx`, `curl … | sh`, other package managers, and common wrappers such as `timeout`, `sudo -u` and `env`.
- **Deliberate evasion is accepted risk R-8.** That covers exported variables, `eval`, commands in script files, planted makefiles, unlisted launchers and similar (the reference list is in issue #29). It is covered instead by:
  - human review of every PR
  - the no-real-data rule (T-607)
  - Socket Firewall and the lockfile policy on the actual installs (T-601)
- Review findings that need deliberate evasion are not P1s. They are recorded against #29.

### Consequences

- Good: the guard stays small and understandable, and PRs converge.
- Bad: an agent that deliberately evades the guard can run an unvetted install on the development machine. With R-4 (in-process egress) and R-6 (dev work and real data on the same machine), this is why no agent may run while real data is accessible.

## References

- THREAT_MODEL T-601, T-605, R-8; ENGINEERING §2.3; issue #29; PR #7 review rounds 1–3
