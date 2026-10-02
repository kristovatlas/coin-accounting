---
status: proposed
date: 2026-10-01
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 601813df126d7baba28709bbaab70b66e6abe46046919bd0a42c10d9aed29913
---

# 0026: actionlint and zizmor as pinned toolchain binaries, with zizmor's online audits

## Context and Problem Statement

ENGINEERING §2.7 and THREAT_MODEL T-603 require `actionlint` and `zizmor` on the workflow files, to catch unsafe patterns and **impostor-commit pins**: a full-SHA `uses:` pin that looks like an action's release but points at a commit that exists only in a fork. Both tools are native binaries (Go and Rust), so ENGINEERING §4.1 requires an ADR, and ADR 0024 covers only four Python tools.

zizmor's online audits need the GitHub API. In v1.30.1 they are `impostor-commit`, `known-vulnerable-actions`, `ref-confusion`, `ref-version-mismatch`, `stale-action-refs` and `typosquat-uses` (the audits in `crates/zizmor/src/audit/` that use its GitHub client; zizmor docs, "Audits"). With `--offline` they don't run, so the impostor-commit check that §2.7 relies on would be silently absent.

Neither project ships a separately signed checksum file. actionlint's `checksums.txt` sits in the same GitHub release as its archives. Both projects do publish **SLSA build provenance** through GitHub artifact attestations. Checked through GitHub's attestations API on 2026-10-01, the pinned archives' digests are attested by `zizmorcore/zizmor` `.github/workflows/release-binaries.yml` at `refs/tags/v1.30.1`, and by `rhysd/actionlint` `.github/workflows/release.yaml` at `refs/tags/v1.7.12`. The signatures were not verified locally: the host `gh` predates `gh attestation verify`.

## Considered Options

1. **Pinned binaries; zizmor runs online with a dedicated no-permission token.**
2. **Pinned binaries; zizmor runs `--offline`,** and §2.7 drops the impostor-commit claim.
3. **No workflow linters.**

## Decision Outcome

Chosen option: **1** (owner's decision on PR #82, 2026-10-01).

- **Pins:** both tools are pinned per platform in `scripts/toolchain.lock` by the SHA-256 of their release archives. They're installed only by `make lint-tools`, through the approval gate like every pin. The archive hash is checked at install; before every run, the installed tree is checked against the digest recorded at install (the T-603 limit: someone who can rewrite both the tree and that record locally isn't caught).
  - **Trust basis: trust-on-first-use against what GitHub served on 2026-10-01**, like `sfw`. The pins were cross-checked when they were made, but every cross-check also came from GitHub: actionlint's `checksums.txt` is in the same release, and the provenance listing was read from GitHub's API without verifying its signatures. A listing is only as trustworthy as whoever could upload it, so it isn't independent evidence until it's verified.
  - Verifying the provenance signatures, with a pinned verifier checking the signer workflow and tag, is a follow-up. It would turn this into verified provenance.
- **No unpinned helpers, no inherited environment:** `make lint-workflows` runs both tools under `env -i` with only `HOME` and a fixed `PATH`, so they inherit no tokens or other secrets. actionlint runs with `-shellcheck= -pyflakes=`, so it never executes a host `shellcheck` or `pyflakes`, and finds `.yml` and `.yaml` workflows itself. zizmor audits the whole repository: workflows, `dependabot.yml` and any local actions.
- **zizmor runs with its online audits on.** That is a **new dev-time network flow**: zizmor to the GitHub API (`api.github.com`), with the names and refs of the actions our workflows use. THREAT_MODEL §6 lists it. It is never a runtime flow.
- **Token handling:**
  - zizmor gets only `ZIZMOR_GITHUB_TOKEN`, which it reads as an alias for `--gh-token` (zizmor v1.30.1, `crates/zizmor/src/cli.rs`). It should be a token with **no permissions**: public-repository read access is all zizmor needs, for rate limits (zizmor docs, "Usage"). The target can't check a token's permissions; that part is up to the owner.
  - Because the environment is otherwise empty, neither tool ever sees the owner's `GH_TOKEN`/`GITHUB_TOKEN`, and `ZIZMOR_OFFLINE`, `ZIZMOR_NO_ONLINE_AUDITS` and `ZIZMOR_CONFIG` can't switch the online audits off.
  - The token's value is briefly visible in `env`'s argument list, to local users who can list processes, until `env` starts zizmor. That's an accepted residual for a no-permission token.
  - With no `ZIZMOR_GITHUB_TOKEN` set, the target fails. zizmor itself would silently fall back to offline (`main.rs`), which is what this guards against.
- **No overrides from the repository being checked:** zizmor runs with `--no-config`, so it loads no `zizmor.yml`. `scripts/check_repo_files.py`, which runs in `make check` and CI, rejects tracked linter config files (`zizmor.yml`, `actionlint.yml` and their `.yaml` forms) and any `zizmor: ignore` comment in any tracked YAML file, local actions included, reading what is staged. `make lint-workflows` runs that check first. A PR can't add an impostor-commit pin and switch that audit off at the same time. Adding a reviewed config means changing that check.
- **CI** gets a step once the pins are on `main` (#94). It will use the workflow token, which is read-only (`contents: read`).

### Consequences

- Good: the impostor-commit check §2.7 promises actually runs, along with the other online audits, and the repository being checked can't switch any of them off.
- Good: one record covers both tools and states their trust basis plainly: hash pins, trust-on-first-use against GitHub, with a provenance listing to verify later.
- Bad: `make lint-workflows` needs network access and a token, and sends GitHub the list of actions we use. That list is already public, in our public workflow files.
- Bad: like every toolchain binary, a compromised actionlint or zizmor runs with the user's file access and network on the development machine (R-6). The empty environment keeps tokens and other secrets out of reach through the environment, but not files such as the `gh` credential store or SSH keys. The hash pin is the real control.

## References

- ENGINEERING §2.3 (toolchain downloads), §2.7 (CI and repository), §4.1 (when an ADR is required)
- THREAT_MODEL T-603, §6, R-6; architecture §9 (this ADR records its new hash, ADR 0014)
- ADR 0013 (supply-chain policy), ADR 0024 (native-code dev tools)
- zizmor docs: https://docs.zizmor.sh/usage/ and https://docs.zizmor.sh/audits/
