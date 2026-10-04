---
status: proposed
date: 2026-10-03
deciders: repository owner (human), drafted by Claude Code
amends: 0018, 0020, 0023
---

# 0030: Cruise mode: a lighter review panel, gated automatic merges and milestone loops

## Context and Problem Statement

The standard process (ADR 0018, 0020, 0023) is careful, but slow. PR #103 (about 650 lines) took four review rounds of four reviewers each, eleven P1 fixes and a human merge. Each round re-reviewed the whole diff and found new Low/Medium items, and several of them became P1s under the broad severity rule. Every PR also pays a fixed cost:
- THREAT_MODEL/ENGINEERING version rows conflicting on every merge
- stacked-PR retargeting
- local full-suite runs
- waiting for the human to merge

The owner wants roughly 5–10× the throughput, and decided (2026-10-02) to relax the process in a way that can be switched off again.

## Considered Options

1. Keep the standard process.
2. Lighter reviews and less per-PR paperwork, with the human still merging every PR (about 3–5×).
3. **Option 2, plus automatic merges behind a mechanical gate for PRs outside the risky paths, plus an agent loop over a whole milestone section.** Everything is switchable back to the standard process with one file.

## Decision Outcome

Option 3 (user decision, 2026-10-02), named **cruise mode**. The switch is the file `PROCESS_MODE` on `main`:
- **`cruise`** turns on everything below.
- **`standard`** restores ADR 0018, 0020 and 0023 unchanged.

Flipping it needs no new ADR, only a PR that the human merges (with the required branch protection, nobody can commit to `main` directly). The stop file and revoking the merge token are the immediate off-switches. Operating it, stopping it and rolling it back are described in [`docs/cruise-mode.md`](../cruise-mode.md). The skills that implement it are `.claude/skills/cruise/SKILL.md` and the cruise profile in `.claude/skills/review-panel/SKILL.md`, both governed by this ADR.

### What changes in cruise mode

1. **The review panel's cruise profile:**
   - **Round 1** reviews the whole diff. **Later rounds review only the fix** (the previous head → the new head), with the files around it for context.
   - **At most 2 rounds.** After round 2, the agent **fixes** every remaining validated P1 that is functional, or a High security finding, with no further review round (user decision, 2026-10-03); the tripwires still run on the fixed SHA. Only a **Critical** security finding, or a committed secret or real user data (which a fix commit can't take out of history), sends the PR to the human as a draft, never to the gate. Only non-P1 findings become issues.
   - **Reviewers by risk:**
     - **All four** when the change touches any tripwire path category, or `chain/`, `tax/` or `doxx/`.
     - **Otherwise two:** one Opus and one Codex, each with a combined security and function focus.
   - **P1 means only:**
     - a Critical/High finding the orchestrator has confirmed
     - a broken build or test, including a credibly flaky test
     - a real leak of secrets or user data
     - wrong tax figures

     Not P1 on its own: a mismatch with a binding document, a gap in a best-effort control, or a Medium.
   - **Records:**
     - one combined comment per round, with each reviewer's report in a collapsed section and the triage
     - one follow-up issue per PR
     - nits stay in the comment
   - **The profile applies only to a run's slices, on a panel the run started** (recorded when the panel state is created). The milestone-closing PR, and any PR the human starts the panel on, get the standard profile.
2. **Gated automatic merge.**
   - At the cruise profile's hand-off (a clean round, or the post-round-2 fixes), when the Opus tripwire check on the full change reports no flags of Medium or above, the orchestrator runs **`main`'s copy of `scripts/cruise_merge.py`**. It merges only if every mechanical condition in its header holds:
     - the local copy of `main` is current, the mode is `cruise`, and there is no stop file
     - the gate is byte-for-byte `main`'s copy
     - `main`'s classic branch protection requires a PR with no required approvals, every gate check and up-to-date branches, applies to administrators too (so the owner's token can't bypass it), and forbids deleting and force-pushing `main`
     - an open, non-draft, same-repository PR based on `main`, from the owner, with every commit authored or committed by the owner, mergeable, at exactly the reviewed SHA, and **containing the current `main`**
     - the repository checks and the **`tests` jobs** (`make test` and `make lint`) succeeded on both platforms, on a complete list of check-runs with none failed. **CI doesn't have the `tests` jobs yet (#44), so until it does, the gate refuses every PR.**
     - no binary file, no file type the gate doesn't scan, nothing under `backend/` or `e2e/` with `tax`, `doxx` or `chain` as a directory or a word in the file name (tests and golden files included), not `domain/secret.py`, the test socket guard or `stub_http.py`, and no package that would shadow any of those or `launcher.py`, `config.py` or `rpc.py`, nor `PROCESS_MODE` or `docs/cruise-mode.md`. Paths are read NUL-separated, with a fixed git configuration, and compared in lower case.
     - no tool configuration that could override the test, lint or build settings (`pytest.toml`, `ruff.toml`, `conftest.py`, `pyproject.toml`, `package.json`, eslint/vite/vitest/playwright configs and similar), in any directory
     - a `cruise/` branch
     - no added dynamic-code name, in any file that can run code, HTML included. This is a **best-effort tripwire**, not a complete control (owner decision, 2026-10-04): deliberate evasion is accepted under R-11. The names include (`eval`, `exec`, `compile`, `getattr`, `vars`, `globals`, `require`, `import(…)`, `__import__`, `__builtins__`, `importlib`, `runpy`, `pickle`, `marshal`, `ctypes`, `sys.modules`, `Reflect.`, `globalThis[…]`/`window[…]`/`self[…]`, `Function` (the gate checks these itself, because the tripwire's dynamic-code label also fires on harmless `re.compile`)
     - no tripwire flag except the two noisy content labels (dynamic code, environment use). That covers:
       - every risky path category: agent instructions, CI, scripts, dependencies, test config, the harness, security-critical modules, binding documents
       - removed test, assertion and guard lines, deleted files, symlinks
       - process, network, download, outside-file and test-weakening content
       - any URL host, loopback included, in code files. `.md` and `.csv` files get only the tripwire's encoded-blob check.
       - any label the tripwire adds later
     - a private merge-token file

     Just before merging, the gate re-reads GitHub's `main` and the stop file, and refuses if either changed.
   - **Other removed code doesn't block.** The tripwire flags only removed test, assertion and guard lines, so a change that deletes ordinary logic outside the blocked paths can merge after review. The owner can make every removed line block later (R-11).
   - Anything else goes to the human, as a **draft** PR, exactly as in standard mode.
   - The merge uses a **separate fine-grained token** with access to this repository only (Contents and Pull requests: read/write), not the owner's everyday credentials. Revoking it stops all automatic merges at once.
3. **Milestone loops.** The human types `/cruise <scope>` (for example `/cruise M0.3`). The loop:
   - plans slices and implements each one as a PR
   - reviews it with the cruise profile, merges it through the gate, and moves on

   It keeps a tracking issue that records:
   - the `main` SHA it started from
   - every PR and merge
   - every design decision it took

   **It stops and notifies the human** when:
   - a dependency is needed
   - an ADR-level decision is needed (ENGINEERING §4.1)
   - a Critical security finding (or a committed secret or real user data) remains after round 2
   - the gate refuses and the PR's follow-up work depends on it
   - CI fails for a reason outside the PR
   - the scope is done

   **The stop file pauses the run**, and removing it resumes the run.
4. **Less per-PR paperwork.**
   - Feature PRs don't add THREAT_MODEL/ENGINEERING version or changelog rows, and don't update threat-model evidence.
   - One **milestone-closing PR** per scope does all of that, plus PLAN and DEPENDENCIES, and the human merges it. It touches binding documents, so the gate always refuses it.
5. **Lighter routine checks.**
   - Manual mutation-checking only for `chain/`, `tax/` and `doxx/`.
   - Low/Medium design questions below the ADR bar may be decided by the agent and logged in the tracking issue, for the human to review in a batch.

### What does not change

These stay with the human:
- supply chain: dependency approval through Socket, installs only through `make`, agents never approve
- no real data, and no secrets
- the network flows F1–F3
- the tax-correctness tests and coverage floors
- the tripwire, the secret scan and the owner-only checks

Changes to agent instructions, CI, scripts, dependencies, security-critical modules and binding documents always go to the human.

### Consequences

- **Good:**
  - The human's time goes to design decisions, risky paths and milestone reviews, not to every merge.
  - Review rounds stop chasing new Lows in unchanged code.
  - Every merge is still traceable to a reviewed SHA, a gate decision and a tracking issue.
- **Bad (new accepted risk R-11):**
  - Code outside the risky paths can reach `main` with no human look. A PR written to steer the AI reviewers and the Opus check, while avoiding every blocking tripwire pattern, could merge.
  - Fewer reviewers and rounds will miss some bugs the standard process would catch.
  - Evidence in the threat model lags the code until the milestone-closing PR.
  - The merge token is another credential on the development machine.
  - The gate's checks are mechanical, and it runs with the owner's read access.

## References

- ADR 0018, 0020, 0023; `scripts/cruise_merge.py`, `scripts/tests/test_cruise_merge.py`; `docs/cruise-mode.md`; `.claude/skills/cruise/SKILL.md`; `.claude/skills/review-panel/SKILL.md`; THREAT_MODEL T-605, R-9, R-11; ENGINEERING §4.1, §6, §8
