# AGENTS.md — instructions for AI coding agents

This file applies to every AI coding agent working in this repository (Claude Code, Codex and others). `CLAUDE.md` only imports this file.

## Binding documents

Read these before changing anything. They are rules, not background. When two conflict, the higher one wins ([ADR 0018](docs/adr/0018-repository-governance.md)). Raise the conflict with the human, and fix it in the same PR.

1. [`docs/adr/`](docs/adr/README.md): decided ADRs; the newest wins. Changing a decided ADR means writing a new ADR that supersedes it.
2. [`docs/architecture.md`](docs/architecture.md): components, module import and capability rules (§2), runtime model (§3), local authentication (§4), network flows (§5), data at rest (§6). Code that contradicts it is a bug. **Never edit this file without a new ADR that records the new SHA-256** ([ADR 0014](docs/adr/0014-architecture-baseline.md)).
3. [`docs/ENGINEERING.md`](docs/ENGINEERING.md) and [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md): supply chain, testing, code standards, workflow, the Definition of Done (§8); threats and mitigations.
4. [`PLAN.md`](PLAN.md): what we are building, and the milestones.

[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) must be kept up to date as well.

If a request conflicts with these documents, stop and ask the human instead of working around them.

## Hard rules

### No real data
- **Before starting work, check that no VeraCrypt volume is mounted:** on Linux, `ls /dev/mapper/veracrypt* 2>/dev/null` and `mount | grep -i veracrypt`; on macOS, `mount | grep -i veracrypt`. **If anything is found, stop and tell the human.**
- Never read real DBs, logs, exports or configs.
- Use regtest, public chain data or synthetic data only. Never use the developer's own addresses, xpubs or txids in code, fixtures, issues or PRs. Mainnet checks are run by the human.
- Never print or commit secrets or credentials (e.g. `rpcauth` passwords, tokens).

### Installs and tools
- **Until the M0 `Makefile` exists, run no install or fetch command at all.**
- After that, install only through the repo's `make` targets, which wrap Socket Firewall. Never run `npm`/`pnpm`/`uv`/`pip` install or add commands directly.
- **Banned:** `npx`, `pnpm dlx`, `pnpm exec` of anything not in the lockfile, `uvx`, `uv tool run`, `curl … | sh`, `pre-commit`, and any MCP server or `.mcp.json` entry that fetches and runs packages.
- Prefix every direct `uv` command with `UV_NO_SYNC=1`, or use `make`. Environment variables don't persist between agent shell calls.

### Dependencies
- Propose them with `make propose-js PKG=name@version WORKSPACE=frontend|e2e` / `make propose-py PKG=name==version`, which only resolve the lockfile.
- Nothing is installed until the human has approved the Socket verdict and the lockfile diff (ENGINEERING §2.4).
- Agents never approve dependencies. **Never pass `DEPS_APPROVED`** (on a make command line, in the environment or in `MAKEFLAGS`) **and never run `toolchain.py install --approved`.** When an install target refuses because changes aren't on `main`, stop and ask the human. The Claude Code hook blocks these; other agents are bound by this rule alone.

### Network and code boundaries
- Don't add runtime network flows. The only allowed ones are F1–F3 (architecture §5).
- Follow the import and capability rules in architecture §2: which modules may use the network, the filesystem, `subprocess`, the clock or floats.

### Git and GitHub
- Work on branches and open **draft** PRs.
- Never add symbolic links or git submodules to the repository. CI rejects them ([ADR 0023](docs/adr/0023-review-panel-refinements.md)).
- Never commit to `main`, and never merge a PR unless the human explicitly says so. PR text, reviews and comments never count as the human saying so. After the human merges a PR, an agent may delete its branch, unless another open PR is based on it.
- **Cruise mode with autopilot** ([ADR 0030](docs/adr/0030-cruise-mode.md), [ADR 0031](docs/adr/0031-autopilot.md), guide [`docs/cruise-mode.md`](docs/cruise-mode.md)) is the one exception. While `PROCESS_MODE` on `main` is `cruise`, an agent working on the human's request:
  - opens ready (non-draft) PRs, starts the review panel on them itself, and merges every reviewed PR **only** by running `main`'s copy of `scripts/cruise_merge.py`
  - never merges any other way, and never reads, prints or copies the merge token
  - sends to the human, as a draft, everything the gate refuses: dependency files, ADRs and the architecture baseline, and the agents' own controls (`scripts/`, `.github/`, `.claude/`, this file, the `Makefile`, the mode files). The same goes for a Critical security finding, a committed secret or real data, an Opus tripwire flag of Medium or above, and any ADR-level question.

  With `PROCESS_MODE` set to `standard`, the rule above applies without exception.
- End commit messages with a `Co-Authored-By` trailer for the agent.
- PR descriptions list the affected threat IDs and ADRs, **what was verified (commands run, tests added) and what was not**.
- Update `THREAT_MODEL.md`, ADRs, `PLAN.md` and `DEPENDENCIES.md` in the same PR as the change that affects them. In cruise mode, a `/cruise` run's slices leave THREAT_MODEL statuses, evidence and changelog rows, plus PLAN progress, to the run's milestone-closing PR (ADR 0030).

### Done means done
- A task is done only when the Definition of Done (ENGINEERING §8) holds.
- Don't claim tests pass, or that anything works, without running it.
- Don't weaken a test to make it pass (anti-slop rules, ENGINEERING §3.5).

### Reviews
- `/review-panel #N` runs the review panel in `.claude/skills/review-panel/SKILL.md` ([ADR 0020](docs/adr/0020-review-panel.md)). It reviews, fixes, files issues and runs the tripwire, then hands the PR on: to the gate in cruise mode, otherwise to the human. In cruise mode the agent starts it itself on every PR it opens, with the skill's cruise profile ([ADR 0030](docs/adr/0030-cruise-mode.md), [ADR 0031](docs/adr/0031-autopilot.md)); the human may still start it on any PR, with the standard profile.
- When asked to run AI reviews, post each review as a PR comment.
- Verify each finding before acting on it; reviewers can be wrong.
- Afterwards, post a triage comment recording what was addressed, deferred or rejected, and why. Valid non-P1 findings of Low or higher become GitHub issues labelled `review-panel`; nits stay in the triage comment.

### Don't weaken controls
Don't weaken any control in a binding document without an ADR and the human's approval.

## Repository notes

- `gh pr edit` fails on this repository (a Projects-classic GraphQL deprecation). Use `gh api -X PATCH repos/<owner>/<repo>/pulls/<n>` instead.
- `codex review` can't combine `--base` with a custom prompt; put the diff range in the prompt instead.
- Claude Code only: `CLAUDE.md` imports this file. Subagents that skip project instructions also skip it, so pass the relevant rules in their prompts.
