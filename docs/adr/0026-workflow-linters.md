---
status: proposed
date: 2026-10-01
deciders: repository owner (human), drafted by Claude Code
---

# 0026: actionlint and zizmor as pinned toolchain binaries, with zizmor's online audits

## Context and Problem Statement

ENGINEERING §2.7 and THREAT_MODEL T-603 require `actionlint` and `zizmor` on the workflow files, to catch unsafe patterns and **impostor-commit pins**: a full-SHA `uses:` pin that looks like an action's release but points at a commit that exists only in a fork. Both tools are native binaries (Go and Rust), so ENGINEERING §4.1 requires an ADR, and ADR 0024 covers only four Python tools.

zizmor's `impostor-commit`, `ref-confusion`, `known-vulnerable-actions` and `typosquat-uses` audits need the GitHub API (zizmor docs, "Audits": these are marked as not working offline). With `--offline` they don't run, so the impostor-commit check that §2.7 relies on would be silently absent.

Neither project ships a separately signed checksum file. actionlint's `checksums.txt` sits in the same GitHub release as its archives. Both projects do publish **SLSA build provenance** through GitHub artifact attestations. Checked through GitHub's attestations API on 2026-10-01, the pinned archives' digests are attested by `zizmorcore/zizmor` `.github/workflows/release-binaries.yml` at `refs/tags/v1.30.1`, and by `rhysd/actionlint` `.github/workflows/release.yaml` at `refs/tags/v1.7.12`. The signatures were not verified locally: the host `gh` predates `gh attestation verify`.

## Considered Options

1. **Pinned binaries; zizmor runs online with a dedicated no-permission token.**
2. **Pinned binaries; zizmor runs `--offline`,** and §2.7 drops the impostor-commit claim.
3. **No workflow linters.**

## Decision Outcome

Chosen option: **1** (owner's decision on PR #82, 2026-10-01).

- **Pins:** both tools are pinned per platform in `scripts/toolchain.lock` by SHA-256. They're installed only by `make lint-tools`, through the approval gate like every pin, and verified (binary hash and tree digest) before every run.
  - The pins were cross-checked when they were made: actionlint against its `checksums.txt`, both against the listed provenance.
  - Verifying the provenance signatures at install time is a follow-up, once a pinned verifier exists.
- **No unpinned helpers:** `make lint-workflows` runs actionlint with `-shellcheck= -pyflakes=`, so it never executes a host `shellcheck` or `pyflakes`.
- **zizmor runs with its online audits on.** That is a **new dev-time network flow**: zizmor to the GitHub API (`api.github.com`), with the names and refs of the actions our workflows use. THREAT_MODEL §6 lists it. It is never a runtime flow.
- **Token handling:**
  - zizmor gets only `ZIZMOR_GITHUB_TOKEN`, a token with **no permissions**: public-repository read access is all zizmor needs, for rate limits (zizmor docs, "Usage").
  - The target unsets `GH_TOKEN`, `GITHUB_TOKEN` and `GH_HOST` for zizmor, so the owner's own token is never handed to it.
  - It also unsets `ZIZMOR_OFFLINE` and `ZIZMOR_NO_ONLINE_AUDITS`, so the online audits can't be switched off from the environment.
  - With no `ZIZMOR_GITHUB_TOKEN` set, the target fails rather than running offline.
- **CI** gets a step once the pins are on `main` (#94). It will use the workflow token, which is read-only (`contents: read`).

### Consequences

- Good: the impostor-commit check §2.7 promises actually runs, along with typosquat, ref-confusion and known-vulnerable-action checks.
- Good: one record covers both tools' trust basis. That basis is a hash pin plus listed provenance; neither is trust-on-first-use in the `sfw` sense.
- Bad: `make lint-workflows` needs network access and a token, and sends GitHub the list of actions we use. That list is already public, in our public workflow files.
- Bad: a compromised zizmor binary would hold a no-permission token. That's harmless beyond rate limits, but it is network access from a third-party binary on the development machine (R-6).

## References

- ENGINEERING §2.3 (toolchain downloads), §2.7 (CI and repository), §4.1 (when an ADR is required)
- THREAT_MODEL T-603, §6, R-6
- ADR 0013 (supply-chain policy), ADR 0024 (native-code dev tools)
- zizmor docs: https://docs.zizmor.sh/usage/ and https://docs.zizmor.sh/audits/
