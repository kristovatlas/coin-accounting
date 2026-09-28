# Dependency Register

Every direct dependency, runtime or development, in every ecosystem, is listed here **before** it is installed. So is every toolchain binary, GitHub Action and GitHub App the project relies on. See [`ENGINEERING.md` §2.4](ENGINEERING.md#24-adding-a-dependency-vet-before-anything-is-installed) for the vetting procedure.

Transitive packages are covered by the lockfiles, the lockfile policy check and the Socket report. They are listed here only when Socket flags them, or when they have network capability, native code or install-time execution.

## Packages

| Name | Ecosystem | Exact version | Runtime / Dev | Purpose | Alternatives considered | Licence | Network capability? | Native code / install scripts? | Socket result | Added (date, PR) | Approved by |
|---|---|---|---|---|---|---|---|---|---|---|---|
| *(none yet)* | | | | | | | | | | | |

## Toolchain and non-package downloads

Verified as described in [`ENGINEERING.md` §2.3](ENGINEERING.md#23-all-installs-go-through-socket-firewall-via-the-repos-scripts).

| Artifact | Version | Platform | SHA-256 (committed) | Verification method | Added (date, PR) |
|---|---|---|---|---|---|
| *(none yet: `sfw`, pnpm, uv, Node.js, Python, `bitcoind`, Playwright browsers)* | | | | | |

## GitHub Actions and Apps

| Name | Kind (Action / App) | Pinned SHA / permissions | Purpose | Added (date, PR) |
|---|---|---|---|---|
| *(none yet: planned Socket GitHub App, Dependabot)* | | | | |

## Cooldown exceptions

Each exception is version-specific, justified by an exploitable vulnerability, and expires within 7 days ([`ENGINEERING.md` §2.6](ENGINEERING.md#26-updating-dependencies)).

| Package@version | Ecosystem | Reason (advisory, exploitability) | Granted (date, PR) | Expires | Removed (date, PR) |
|---|---|---|---|---|---|
| *(none)* | | | | | |

## Removed dependencies

| Name | Ecosystem | Removed (date, PR) | Reason |
|---|---|---|---|
| *(none yet)* | | | |
