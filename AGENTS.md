# AGENTS.md — instructions for AI coding agents

This file applies to every AI coding agent working in this repository (Claude Code, Codex and others). `CLAUDE.md` only imports this file.

## Binding documents

Read these before changing anything. They are rules, not background:

1. [`PLAN.md`](PLAN.md): what we are building, and the milestones.
2. [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md): assets, threats and mitigations. Update it **in the same PR** as any change that affects a boundary, asset, store, network flow, dependency or tax rule.
3. [`docs/ENGINEERING.md`](docs/ENGINEERING.md): supply chain, testing, code standards, workflow and the Definition of Done.
4. [`docs/architecture.md`](docs/architecture.md): components, module import rules, network flows, data at rest. Code that contradicts it is a bug.
5. [`docs/adr/`](docs/adr/): accepted decisions. Changing one means writing a new ADR that supersedes it.

If a request conflicts with these documents, stop and ask the human instead of working around them.

## Hard rules

- **No real data.** Never run while a VeraCrypt volume with real data is mounted. Never read real DBs, logs, exports or configs. Use regtest and synthetic data only. Never use real addresses, xpubs or txids in code, fixtures, issues or PRs. Mainnet checks are run by the human.
- **Installs:** only through the repo's `make` targets, which wrap Socket Firewall. Never run `npm`/`pnpm`/`uv`/`pip` install or add commands directly, and never `npx`, `pnpm dlx`, `uvx`, `curl | sh` or `pre-commit`. Always keep `UV_NO_SYNC=1` set.
- **Dependencies:** propose them with `make propose-js` / `make propose-py`, which only resolve the lockfile. Nothing is installed until the human has approved the Socket verdict and the lockfile diff (ENGINEERING §2.4). Agents never approve dependencies.
- **Network:** don't add runtime network flows. The only allowed ones are F1–F3 (architecture §3). Network-capable code lives only in the modules listed in ENGINEERING §5.2.
- **Git and GitHub:**
  - Work on branches and open **draft** PRs.
  - Never commit to `main`, and never merge a PR unless the human explicitly says so.
  - End commit messages with a `Co-Authored-By` trailer for the agent.
  - PR descriptions list the affected threat IDs and ADRs, and what was verified.
- **Reviews:** when asked to run AI reviews, post each review as a PR comment, and afterwards a triage comment recording what was addressed, deferred or rejected, and why.
- **Tests:** follow the E2E-first strategy and the anti-slop rules (ENGINEERING §3). Don't weaken a test to make it pass.
- **Don't weaken controls** in any binding document without an ADR and the human's approval.

## Repository notes

- `gh pr edit` fails on this repository (a Projects-classic GraphQL deprecation). Use `gh api -X PATCH repos/<owner>/<repo>/pulls/<n>` instead.
- `codex review` can't combine `--base` with a custom prompt; put the diff range in the prompt instead.
