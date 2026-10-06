# Cruise mode: operator guide

Cruise mode ([ADR 0030](adr/0030-cruise-mode.md)), with autopilot ([ADR 0031](adr/0031-autopilot.md)), is the faster way of developing this project:
- a lighter review panel, which the agent starts itself on every PR it opens
- automatic merges behind a mechanical gate, for every reviewed PR except human decisions and the agents' own controls
- an agent loop that works through a whole milestone section

The standard process ([ADR 0018](adr/0018-repository-governance.md), [0020](adr/0020-review-panel.md), [0023](adr/0023-review-panel-refinements.md)) stays defined, and one file switches between the two.

## The switch

`PROCESS_MODE` at the repository root, on `main`:

| Value | Effect |
|---|---|
| `cruise` | ADR 0030 and ADR 0031 (autopilot) apply: the cruise panel profile, gated merges of every PR the panel cleared, `/cruise` loops |
| `standard` | ADR 0018/0020/0023 apply unchanged; the gate refuses every merge |

The gate and the skills read the value **from `origin/main`**, never from a PR branch, so a PR can't switch the mode for itself.

## One-time setup (before the first automatic merge)

1. **Create a fine-grained personal access token** (GitHub → Settings → Developer settings → Fine-grained tokens):
   - Repository access: **only `coin-accounting`**.
   - Permissions: **Contents: Read and write**, **Pull requests: Read and write**, and Metadata: Read (set automatically). Nothing else, and in particular no Administration or Workflows permission.
   - Expiry: short, for example 30 days. Renew it while cruise mode is in use.
2. **Store it** where the gate looks for it, readable only by you:

   ```sh
   mkdir -p ~/.config/coin-accounting && chmod 700 ~/.config/coin-accounting
   ( umask 077; cat > ~/.config/coin-accounting/cruise-merge-token )   # paste the token, then Ctrl-D
   ```

   Another path works too: set `CRUISE_MERGE_TOKEN_FILE`. Without the file, the gate refuses every merge and PRs simply come to you.
3. **Allow the gate in auto mode.** Add this rule to your local, uncommitted `.claude/settings.local.json`, replacing `<repo>` with the absolute path of your checkout. It lets the auto-mode classifier allow a run's call to that one path:

   ```json
   { "permissions": { "allow": ["Bash(python3 <repo>/.git/cruise/gate.py:*)"] } }
   ```

   The run always writes `main`'s copy of `scripts/cruise_merge.py` to that path, and calls it with the path written out. Other code could write to that path too; that's within R-9 (agents aren't sandboxed). Don't keep broader rules such as `Bash(python3 *)` or `Bash(gh pr *)`: they would allow merging without the gate. Remove the rule to make every merge need your approval again.
4. **Required: protect `main`.** GitHub → Settings → Branches → **Add classic branch protection rule** for `main` (the gate reads the classic rules; a ruleset isn't visible to it):
   - require a pull request before merging
   - require status checks to pass: `checks (ubuntu-latest)`, `checks (macos-latest)`, `tests (ubuntu-latest)` and `tests (macos-latest)`
   - leave "Require approvals" **off** (0 approvals): the token can't approve its own PR
   - leave "Allow deletions" and "Allow force pushes" **off**
   - require branches to be up to date before merging
   - **do not allow bypassing the above settings** (it applies to administrators, so your token can't skip it)

   This stops the token from pushing to `main` directly. The gate refuses until all of this is in place, including the two `tests` checks.
5. **CI must run the tests.** The `tests (…)` jobs, which run `make test` and `make lint` on both platforms, come from CI's `tests` job (#120).
6. **Optional: notifications.** Put a hard-to-guess ntfy topic in `~/.config/coin-accounting/ntfy-topic` (mode 600; runs keep it off the command line). Without it, runs don't notify. The topic is never committed: ntfy topics work like shared secrets.

## Starting a run

In Claude Code, in this repository:

```
/cruise M0.3
```

The argument is a scope from `PLAN.md`: a milestone (`M1`), a section of one (`M0.3`), or a short description in quotes. The loop:
1. opens a tracking issue, **"Cruise: <scope>"**, recording the `main` SHA it starts from
2. plans the slices, and posts the plan in the issue
3. for each slice:
   - implements it on a branch from `main`
   - opens a ready (non-draft) PR
   - reviews it with the cruise panel profile (at most 2 rounds)
   - runs the gate. **Merged:** it continues. **Refused:** the PR becomes a draft for you, and the loop continues with work that doesn't depend on it.
4. ends with a **milestone-closing PR**, which merges through the gate like any other (autopilot). It holds the THREAT_MODEL statuses, evidence and changelog row, the ENGINEERING row if needed, PLAN and DEPENDENCIES.

It runs from a 10-minute check-in job in that Claude Code session. The job expires after 7 days, or when the session ends, and `/cruise <scope>` resumes it from its saved state. It sends an ntfy notification whenever it needs you, and when it finishes.

## Stopping

| How | Effect |
|---|---|
| `touch "$(git rev-parse --path-format=absolute --git-common-dir)/cruise-stop"` | **Immediate pause.** The gate refuses every merge, and the run pauses at its next tick, keeping its state. Delete that file, and the next tick carries on. (In the main checkout, that's `.git/cruise-stop`.) |
| `/cruise stop` | The loop stops after its current step and removes its check-in job; open PRs stay open. |
| Revoke the merge token on GitHub | Automatic merges stop at once, everywhere, even if a session ignores the stop file. |
| Set `PROCESS_MODE` to `standard` on `main` | Back to the standard process; see below. |

## Rolling back

1. **Return to the standard process.**
   - Change `PROCESS_MODE` to `standard` in a one-line PR and merge it yourself. Branch protection rules out committing to `main` directly, even for you. If CI is broken and you must lift the protection to merge it, **revoke the merge token first** (protection is what stops the token pushing to `main`), restore the protection right after, and only then issue a new token.
   - For an immediate stop, use the stop file or revoke the token first (see **Stopping**).
   - No ADR is needed: ADR 0030 defines both values.
   - From then on, the review panel runs its standard profile, nothing merges automatically, and `/cruise` refuses to start.
2. **Review or undo what cruise mode merged.**
   - Each run's tracking issue lists the starting SHA and every PR it merged. Every automatic merge commit's title ends in "(autopilot, ADR 0031)" (before autopilot, "(cruise mode, ADR 0030)"), so `git log --merges -E --grep "\((autopilot, ADR 0031|cruise mode, ADR 0030)\)"` finds them.
   - To see what each merge changed: `git diff <merge-sha>^1 <merge-sha>`, for the merge SHAs listed in the run's tracking issue. A plain `git diff <start-sha> main` would also include other PRs merged in the meantime.
   - To undo one merge, use a PR containing `git revert -m 1 <merge-sha>`. To undo a run, revert its merges newest first.
3. **Catch up the paperwork.** If a run stopped before its milestone-closing PR, ask for one, or write it yourself. It brings THREAT_MODEL evidence and the changelog up to date.
4. **Remove cruise mode entirely** (optional). That needs a new ADR that supersedes ADR 0030, plus deleting:
   - `PROCESS_MODE`
   - `scripts/cruise_merge.py` and its test
   - `.claude/skills/cruise/`
   - the cruise profile in the review-panel skill
   - this guide

## What merges automatically, and what comes to you

**Merges automatically** (autopilot, ADR 0031), after a clean cruise review, an Opus tripwire with no flag of Medium or above, and green CI:
- application code, including the security-critical modules (`api/`, the launcher, `storage/`, `rpc.py`, `config.py`) and the `tax/`, `doxx/` and `chain/` engines
- tests, golden files, E2E specs and build configuration other than `vite.config.*` (`tsconfig.json`, `playwright.config.*`)
- the living binding documents: `THREAT_MODEL.md`, `ENGINEERING.md`, `PLAN.md`, `DEPENDENCIES.md`
- the milestone-closing PR
- PRs outside a `/cruise` run: the review panel runs the gate itself

The panel marks a commit it cleared with the commit status `review-panel`, and the gate refuses a commit without it. Every merge is listed in the run's tracking issue, or, outside a run, in the standing **"Autopilot merges"** issue. The mechanical tripwire's path, content, removed-line and deleted-file flags are posted for the record; symlinks, submodules, executable bits, changes it can't parse and any new kind of flag block.

**Always comes to you,** whatever the mode:
- dependency approvals, and any change to dependency manifests, lockfiles or install configuration (`uv.toml`, `.npmrc`, `.python-version`, …), in any directory
- ADR-level decisions, ADRs, and `docs/architecture.md`
- changes to the agents' own controls: `scripts/`, `.github/`, the `Makefile`, `PROCESS_MODE`, this guide; agent instructions, skills and tool configuration in any directory (`.claude/`, `.codex/`, `AGENTS*.md`, `CLAUDE*.md`, `.mcp.json`); the test socket guard, every `conftest.py` and a `pytest.toml`/`pytest.ini`; the lint, type-check, coverage and mutation settings (`ruff.toml`, `mypy.ini`, `.coveragerc`, `eslint.config.*`, `vitest.config.*`, `vite.config.*`, `mutation-exclusions.md`); other agents' and editors' configuration (`GEMINI.md`, `.vscode/`, `.windsurf/`, `.devcontainer/`, `.zed/`, …); Socket's `socket.yml`; the regtest harness (`e2e/harness/`); type stubs (`*.pyi`); any new file or directory at the top of the repository, or Python module directly under `backend/` or `e2e/` (it could stand in for a test tool); vendored or minified third-party code; deleting or renaming a file in the `tax/`, `doxx/` or `chain/` engines
- a change that weakens a control in a binding document (it needs an ADR)
- a Critical security finding, or a committed secret or real user data, found in **any** round: the panel labels the PR `autopilot-blocked`, and only you remove that label (functional and High security P1s are fixed during the run)
- any Opus tripwire flag of Medium or above, any "binding-document control", "third-party code" or "check suppression" flag, and anything else the gate refuses
