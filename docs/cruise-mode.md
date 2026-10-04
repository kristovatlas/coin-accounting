# Cruise mode: operator guide

Cruise mode ([ADR 0030](adr/0030-cruise-mode.md)) is the faster way of developing this project:
- a lighter review panel
- automatic merges behind a mechanical gate, for PRs outside the risky paths
- an agent loop that works through a whole milestone section

The standard process ([ADR 0018](adr/0018-repository-governance.md), [0020](adr/0020-review-panel.md), [0023](adr/0023-review-panel-refinements.md)) stays defined, and one file switches between the two.

## The switch

`PROCESS_MODE` at the repository root, on `main`:

| Value | Effect |
|---|---|
| `cruise` | ADR 0030 applies: the cruise panel profile, gated merges, `/cruise` loops |
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
   - require status checks to pass: `checks (ubuntu-latest)` and `checks (macos-latest)`. **Add `tests (ubuntu-latest)` and `tests (macos-latest)` only once the CI tests PR (#44) is merged**: before that, they never report, and nothing could merge, your own PRs included
   - leave "Require approvals" **off** (0 approvals): the token can't approve its own PR
   - leave "Allow deletions" and "Allow force pushes" **off**
   - require branches to be up to date before merging
   - **do not allow bypassing the above settings** (it applies to administrators, so your token can't skip it)

   This stops the token from pushing to `main` directly. The gate refuses until all of this is in place, including the two `tests` checks, so it merges nothing before #44.
5. **CI must run the tests.** The `tests (…)` jobs, which run `make test` and `make lint` on both platforms, come from a separate CI PR (#44). Until it's merged, the gate refuses every PR, and runs hand every slice to you as a draft: still reviewed, but not merged automatically.
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
4. ends with a **milestone-closing PR**, which you merge. It holds the THREAT_MODEL statuses, evidence and changelog row, the ENGINEERING row if needed, PLAN and DEPENDENCIES.

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
   - Change `PROCESS_MODE` to `standard` in a one-line PR and merge it yourself. Branch protection rules out committing to `main` directly, even for you. If CI is broken, lift the protection temporarily to merge it.
   - For an immediate stop, use the stop file or revoke the token first (see **Stopping**).
   - No ADR is needed: ADR 0030 defines both values.
   - From then on, the review panel runs its standard profile, nothing merges automatically, and `/cruise` refuses to start.
2. **Review or undo what cruise mode merged.**
   - Each run's tracking issue lists the starting SHA and every PR it merged. Every automatic merge commit's title ends in "(cruise mode, ADR 0030)", so `git log --merges --fixed-strings --grep "(cruise mode, ADR 0030)"` finds them.
   - To see what each merge changed: `git diff <merge-sha>^1 <merge-sha>`, for the merge SHAs listed in the run's tracking issue. A plain `git diff <start-sha> main` would also include other PRs merged in the meantime.
   - To undo one merge, use a PR containing `git revert -m 1 <merge-sha>`. To undo a run, revert its merges newest first.
3. **Catch up the paperwork.** If a run stopped before its milestone-closing PR, ask for one, or write it yourself. It brings THREAT_MODEL evidence and the changelog up to date.
4. **Remove cruise mode entirely** (optional). That needs a new ADR that supersedes ADR 0030, plus deleting:
   - `PROCESS_MODE`
   - `scripts/cruise_merge.py` and its test
   - `.claude/skills/cruise/`
   - the cruise profile in the review-panel skill
   - this guide

## What always comes to you

Whatever the mode:
- dependency approvals
- ADR-level decisions
- changes to agent instructions, CI, scripts, dependency files, security-critical modules (the tripwire's list) or binding documents
- any PR the gate refuses
- a Critical security finding, or a committed secret or real user data, after round 2 (functional and High security P1s are fixed during the run)
- any Opus tripwire flag of Medium or above, and anything else the gate refuses (ADR 0030 lists the conditions)
- the milestone-closing PR
