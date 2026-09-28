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
- Never commit to `main`, and never merge a PR unless the human explicitly says so.
- End commit messages with a `Co-Authored-By` trailer for the agent.
- PR descriptions list the affected threat IDs and ADRs, **what was verified (commands run, tests added) and what was not**.
- Update `THREAT_MODEL.md`, ADRs, `PLAN.md` and `DEPENDENCIES.md` in the same PR as the change that affects them.

### Done means done
- A task is done only when the Definition of Done (ENGINEERING §8) holds.
- Don't claim tests pass, or that anything works, without running it.
- Don't weaken a test to make it pass (anti-slop rules, ENGINEERING §3.5).

### Reviews
- When asked to run AI reviews, post each review as a PR comment.
- Verify each finding before acting on it; reviewers can be wrong.
- Afterwards, post a triage comment recording what was addressed, deferred or rejected, and why.

### Don't weaken controls
Don't weaken any control in a binding document without an ADR and the human's approval.

## Repository notes

- `gh pr edit` fails on this repository (a Projects-classic GraphQL deprecation). Use `gh api -X PATCH repos/<owner>/<repo>/pulls/<n>` instead.
- `codex review` can't combine `--base` with a custom prompt; put the diff range in the prompt instead.
- Claude Code only: `CLAUDE.md` imports this file. Subagents that skip project instructions also skip it, so pass the relevant rules in their prompts.
