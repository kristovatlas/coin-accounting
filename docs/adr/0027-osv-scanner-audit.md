---
status: proposed
date: 2026-10-01
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 95fec83b6ad8bf68a0ab01ea9d82430e9403addcf108bdbc1329ea8026c3f361
---

# 0027: osv-scanner as the pinned vulnerability audit, with a dev-time flow to OSV

## Context and Problem Statement

ENGINEERING §2.6 and THREAT_MODEL T-601 require a vulnerability audit of the lockfiles (`make audit`). The plan was `pip-audit` plus `pnpm audit`, but `pip-audit` brings a heavy Python dependency tree (`requests`, `cyclonedx` and more). `osv-scanner` (Google) covers both `uv.lock` and `pnpm-lock.yaml` as a single Go binary that can be pinned like the toolchain. It is a native binary that uses the network, so ENGINEERING §4.1 requires an ADR.

osv-scanner sends the lockfiles' package names and versions to the OSV API (`api.osv.dev`). By default it also loads an `osv-scanner.toml` from the folder of each file it scans, and that file's `IgnoredVulns` and `PackageOverrides` entries hide findings (osv-scanner docs, "Configuration"). Its transitive resolution of manifest files can also query deps.dev or registries (the `--data-source` and `--no-resolve` flags, v2.6.0 `cmd/osv-scanner`).

## Considered Options

1. **Pinned osv-scanner binary,** run with an empty config, no resolution and an empty environment.
2. **`pip-audit` and `pnpm audit`,** as dev dependencies run through `sfw`.
3. **No local audit;** rely on Socket and Dependabot alerts.

## Decision Outcome

Chosen option: **1** (proposed in PR #85).

- **Pin:** `osv-scanner` 2.6.0 is pinned per platform in `scripts/toolchain.lock` by the SHA-256 of its release binaries. It's installed only by `make audit-tools`, through the approval gate like every pin. The binary's hash is checked at install, and the installed tree is checked against its install-time digest before every run (the T-603 limit applies).
  - **Trust basis: trust-on-first-use against what GitHub served on 2026-10-01,** like actionlint and zizmor (ADR 0026). The digests match the publisher's `osv-scanner_SHA256SUMS`, but that file is in the same GitHub release.
  - The release also ships SLSA provenance (`multiple.intoto.jsonl`), which hasn't been verified. Verifying it with a pinned verifier is a follow-up that would turn this into verified provenance.
- **No overrides from the repository being audited:**
  - `make audit` passes `--config` with an empty file it creates outside the repository, so no `osv-scanner.toml` is loaded from any directory.
  - `scripts/check_repo_files.py` (in `make check`, CI and at the start of `make audit`) also rejects a tracked `osv-scanner.toml` in any letter case, anywhere in the tree.
  - Ignoring a finding means changing that check, in a reviewed PR.
- **One flow:** `--no-resolve` turns off transitive resolution, which isn't needed because our lockfiles are complete. That keeps deps.dev and the registries out, and the only network flow is **`make audit` → `api.osv.dev`**: the package names and versions from the local lockfiles, and the requester's IP address. It's a **dev-time flow** (THREAT_MODEL §6, architecture §9), never a runtime one. For merged lockfiles this is the same information the public repository already shows. A local run on an unpushed branch also reveals package choices not yet published.
- **No inherited environment:** osv-scanner runs under `env -i` with only `HOME` and a fixed `PATH`. So it sees no tokens (`GH_TOKEN`, `GITHUB_TOKEN`, `ZIZMOR_GITHUB_TOKEN`) and no proxy or CA variables.
- **Exit status:** the scan's exit status is the target's, so findings fail `make audit`.
- **CI** gets an audit step once the pin is on `main` (tracked with #94's CI step). Until then the "`make audit` on every PR" control in ENGINEERING §2.7 is planned, not enforced.

### Consequences

- Good: one tool audits both ecosystems, with no added Python dependency tree, and the repository being audited can't switch findings off.
- Bad: a new dev-time flow to Google's OSV service, carrying the lockfiles' package names and versions and the developer's IP address.
- Bad: like every toolchain binary, a compromised osv-scanner runs with the user's file access and network on the development machine (R-6). The empty environment keeps secrets that live in environment variables out of reach, but not secrets in files. The hash pin is the real control.

## References

- ENGINEERING §2.3 (toolchain downloads), §2.6 and §2.7 (audit on every PR), §4.1 (when an ADR is required)
- THREAT_MODEL T-601, T-603, §6, R-6; architecture §9 (this ADR records its new hash, ADR 0014)
- ADR 0013 (supply-chain policy), ADR 0026 (the same pattern for the workflow linters)
- osv-scanner docs: https://google.github.io/osv-scanner/configuration/ and https://google.github.io/osv-scanner/usage/offline-mode/
