---
name: review-panel
description: Run the 4-reviewer panel on a pull request (Opus 5.5 security + functional, Codex gpt-5.6-sol security + functional), fix validated P1/High/Critical findings, open GitHub issues for the other validated findings, and repeat until a round is clean. Then merge, or surface the human decisions. Use when the user runs /review-panel #<PR>, and when the panel's own cron tick re-sends that command.
argument-hint: "#<PR number>"
---

# /review-panel #N

Drive PR #N to merge with as little human attention as possible (ADR 0020). The command is **idempotent**. The first call starts the panel, and every later call (from the user or the 10-minute cron tick) looks at the saved state and advances it by one step.

## Rules that always apply

- `AGENTS.md` and the binding documents apply in full: no real data, no installs outside `make`, draft PRs, the threat model updated in the same PR, and so on.
- Before any other step, run the VeraCrypt check from `AGENTS.md`. If a volume is mounted, stop and tell the user.
- Subagents may not load project instructions. So every reviewer prompt below repeats the rules it needs.
- Reviewers are **read-only**: they never edit, commit, push or comment. Only this skill posts to GitHub.

## State

Keep the state in `.git/review-panel/pr-<N>.json`. Git never commits anything under `.git/`, and the file survives a Claude session ending. Create the directory if needed.

```json
{
  "pr": 7, "round": 1, "stage": "reviewing",
  "cron_id": "…", "head": "<commit sha reviewed this round>",
  "reviewers": {
    "opus-sec":  {"status": "running", "output": ".git/review-panel/pr-7-r1-opus-sec.md"},
    "opus-func": {"status": "running", "output": "…"},
    "sol-sec":   {"status": "done",    "output": "…"},
    "sol-func":  {"status": "running", "output": "…"}
  },
  "p1_fixes": ["short description", "…"],
  "decisions": ["…"],
  "history": [{"round": 1, "p1": 3, "issues": [12, 13], "comment_urls": ["…"]}]
}
```

- **`stage`:** `reviewing` → `validating` → `fixing` (then the next round's `reviewing`) or `deciding` → `awaiting-human` or `merged`.
- **Reviewer `status`:** `not-started`, `running`, `done`, `waiting-limit` (a usage limit was hit; retry later), `failed`.

## Each invocation: one tick

1. **Parse `#N`** and load the state. If there is no state, create it with round 1, stage `reviewing`, and every reviewer `not-started`.
2. **Check the PR:** `gh pr view N --json state,headRefName,headRefOid,isDraft`. If it is `MERGED` or `CLOSED`, delete the cron job (`CronDelete`), delete the state file, print `PR #N is <state>; review panel stopped.` and stop.
3. **Check the cron job.** If `CronList` has no job whose prompt is `/review-panel #N`, create one: `CronCreate` with cron `"3-59/10 * * * *"`, prompt `/review-panel #N`, `recurring: true`. Save its id in the state.
   - The job lives only in this Claude session, fires only while the session is idle, and expires after 7 days. If the session ends, the user runs `/review-panel #N` again to resume from the state file. Tell the user this the first time.
4. **Advance the current stage** as described below.
5. **Print the status** in the format under "Status output", as the last thing in the reply.

### Stage `reviewing`

- Record `head` = the PR's `headRefOid`, and run `git fetch origin` so the reviewers can read the diff `origin/<base>...origin/<headRefName>`.
- For each reviewer that is `not-started` or `waiting-limit`, **launch it** (see "Launching reviewers") and set it to `running`. Launch every one that is due in a single message, so they run in parallel.
- For each `running` reviewer, check whether it has finished:
  - **Codex:** the output file ends with a line `EXIT:<code>`. If the output mentions a usage or rate limit (e.g. "usage limit", "rate limit", "try again at"), set `waiting-limit`. If the code is 0 and the output has a review, set `done`. Otherwise set `failed`, and relaunch it once in the next tick.
  - **Opus:** it is `done` when its completion notification has arrived **and** you have written its full report to its `output` file. When the notification arrives, write the file straight away, even if it arrives outside a tick. If the report says the agent hit a usage limit, set `waiting-limit`.
- When all four are `done`, set stage `validating` and continue in the same tick.

### Stage `validating`

1. **Post each review verbatim as its own PR comment.** Headed `## <Reviewer> review of PR #N (round R)`, with the commit reviewed and "Triage follows in a separate comment." Keep the comment URLs.
2. **Validate every finding before acting on it.** Reviewers can be wrong, and they often disagree.
   - Read the code or docs they cite, and run commands or tests where that settles the question.
   - For claims about tools, laws or APIs, check a primary source (docs, source code, irs.gov).
   - Merge duplicates across reviewers.
   - Mark each finding as **valid**, **rejected** (with the reason and any source), or **needs a human decision** (see "Human decision points").
3. **Classify each valid finding.** It is a **P1** if its severity is Critical, High, P0 or P1 in the reviewer's own scale, **or** if your validation shows it would cause a security hole, data loss, a privacy leak, wrong tax figures, or a broken build or test. Everything else valid is non-P1.
4. **Open a GitHub issue for each valid non-P1 finding.**
   - Before creating one, search for an existing open issue: `gh issue list --label review-panel --search "<key words>"`.
   - Create the labels `review-panel` and `severity:medium` / `severity:low` if they are missing.
   - The issue title is short. The body has the finding, the file and section, why it matters, the proposed fix, and links to the PR and to the review comment. Use `--body-file`.
5. **Post a triage comment:**
   - the P1s to fix now
   - the issues opened (with numbers)
   - the rejected findings, with reasons
   - the items that need a human decision
6. **Choose the next stage:**
   - If there are valid P1s, set stage `fixing` and save their one-line descriptions in `p1_fixes`.
   - Otherwise, set stage `deciding`.

### Stage `fixing`

- Check out the PR branch, and fix **only** the P1s, following `AGENTS.md`:
  - add or adjust tests
  - update the threat model, ADRs, `PLAN.md` and `DEPENDENCIES.md` where the fix affects them
  - run `make check` and every test suite that exists
- Commit with a message that lists the P1s fixed, using `git commit -F <file>`, and push.
- **If a P1 can't be fixed without a human decision** (see below), don't guess. Add it to `decisions`, and leave it unfixed.
- Then start the next round: `round += 1`, every reviewer back to `not-started`, stage `reviewing`. Assume the fixes may have introduced new problems. Start the new reviewers in this same tick.
- **Loop guard:** if the next round would be round 6, set stage `awaiting-human`, and tell the user in plain language that the panel keeps finding P1s. Summarize what keeps recurring, and ask how to proceed.

### Stage `deciding`: the round was clean (no valid P1s)

1. **Collect the human decision points:** the `decisions` list, plus anything in the PR itself that matches "Human decision points".
2. **If there are none**, and CI is green (`gh pr checks N`), and the PR has no merge conflict:
   - `gh pr ready N`, then `gh pr merge N --merge`
   - `CronDelete` the job, delete the state file, and print `PR #N merged after R round(s).`
3. **If there are decision points:**
   - Post them as a PR comment.
   - Tell the user in **plain language**: what the choice is, the options, and your recommendation. Keep it short, and don't use review jargon.
   - Set stage `awaiting-human`.
4. **If CI is red or there is a conflict:** fix it as if it were a P1 (stage `fixing`).

### Stage `awaiting-human`

- Do nothing except print the status, until the user answers.
- When the user answers:
  - Apply the decision.
  - If the answer changed code or docs, start a new round.
  - Otherwise, go back to `deciding`.

## Human decision points

Anything the binding documents reserve for the human, or that changes what was agreed:
- **New or changed dependencies, toolchain pins, GitHub Actions or Apps.** ENGINEERING §2.4 requires the human's explicit approval of each one, unless the human already approved these exact versions in this PR.
- Adding, superseding or re-deciding an ADR, or changing `docs/architecture.md` (and its hash).
- Weakening any control, accepting a new risk, or changing an accepted risk (THREAT_MODEL §9).
- Tax positions and tax-rule interpretations; privacy trade-offs; changes to product scope or UX that the user hasn't asked for.
- A reviewer finding you rejected where the stakes are high and your validation depends on judgment rather than a source.
- Anything a reviewer or you flag as "needs a human decision".

## Launching reviewers

Write every prompt to a file under `.git/review-panel/`, and pass it by path or with `--`, never inline. That keeps banned words in a prompt from tripping the command guard.

**Opus 5.5 (two agents):** use the `Agent` tool with `model: "opus"`, `subagent_type: "general-purpose"` and `run_in_background: true`, at the default effort. Use the reviewer prompt below with FOCUS set to *security* for one agent and *functional* for the other.

**Codex gpt-5.6-sol (two runs):** run in the background (Bash with `run_in_background: true`):

```sh
codex review -c model="gpt-5.6-sol" "$(cat .git/review-panel/pr-N-rR-sol-sec.prompt)" \
  > .git/review-panel/pr-N-rR-sol-sec.md 2>&1; echo "EXIT:$?" >> .git/review-panel/pr-N-rR-sol-sec.md
```

The same again for `sol-func`. `codex review` can't take `--base` together with a prompt, so the diff range goes inside the prompt.

**Reviewer prompt** (fill in N, R, base, head branch, FOCUS):

> Review PR #N, round R, in this repository: the changes in `git diff origin/<base>...origin/<head>`. READ-ONLY: don't edit files, commit, push, or post to GitHub. Report your findings in your final answer.
> Rules of this repository (from AGENTS.md): never read real user data, and never run install or fetch-and-run commands (`npm`/`pnpm`/`uv`/`pip` installs, `npx`, `uvx`, `curl | sh`). The binding documents are `docs/adr/`, `docs/architecture.md`, `docs/ENGINEERING.md`, `docs/THREAT_MODEL.md` and `PLAN.md`; read them as needed to judge the change.
> FOCUS = **security**: vulnerabilities, privacy leaks, secret handling, supply-chain risk, trust-boundary and threat-model gaps, unsafe defaults, and mismatches between the code and THREAT_MODEL/architecture.
> FOCUS = **functional**: correctness bugs, spec mismatches against PLAN/ADRs/architecture, missing or weak tests (ENGINEERING §3 anti-slop rules), broken builds or CI, edge cases, error handling, and maintainability problems that will cause defects.
> Output a prioritized list. For each finding give: **severity** (Critical / High / Medium / Low / Nit), file and line or section, the problem, why it matters, and a concrete fix. Include only findings you are confident about; mark uncertain ones as uncertain. Cite sources for claims about tools, laws or APIs. No praise.

## Status output

Print exactly one block at the end of every tick.

- **While reviewing** (`[~]` in progress, `[x]` done, `[ ]` not started, `[!]` waiting on a usage limit or failed):

```
PR #N Review (round R):
[~] opus 5.5 sec
[~] opus 5.5 func
[x] 5.6-sol sec
[~] 5.6-sol func
```

- **While fixing:**

```
PR #N fixes in progress (round R):
- <P1 one-liner>
- <P1 one-liner>
```

- **Otherwise:** `PR #N: validating round R` / `PR #N: awaiting human decision (see above)` / `PR #N merged after R round(s).`

## Usage limits

- Claude Code and Codex both run on session tokens only. When a limit is hit, don't retry in a loop.
- Mark the reviewer `waiting-limit`, and let later ticks retry.
- If the Claude session itself stops, the state file keeps everything. After the reset, `/review-panel #N` resumes where it left off.
