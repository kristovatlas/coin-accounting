# Architecture Decision Records

MADR-format records of significant decisions (see [ADR 0001](0001-record-decisions-with-adrs.md) and ENGINEERING §4).
- **Status is in each file's front matter.** Any ADR on `main` has been decided by the human's merge.
- This index lists titles only. CI will regenerate and check it (M0).
- When binding documents conflict, [ADR 0018](0018-repository-governance.md) sets which one wins.

| ADR | Title |
|---|---|
| [0001](0001-record-decisions-with-adrs.md) | Record significant decisions as ADRs |
| [0002](0002-local-web-app.md) | Local web app (FastAPI backend + React SPA on loopback) |
| [0003](0003-supported-platforms.md) | Supported platforms are Linux and macOS |
| [0004](0004-node-access.md) | Node access via loopback JSON-RPC with a whitelisted rpcauth user |
| [0005](0005-chain-data-via-core-indexes.md) | Chain data from Core's own indexes; no app-side chain index |
| [0006](0006-storage-on-veracrypt-volume.md) | The app stores its data only on the VeraCrypt volume |
| [0007](0007-price-data-bulk-download.md) | Fiat prices by bulk, date-independent download |
| [0008](0008-per-account-basis-and-identification.md) | Lot identification in every account |
| [0009](0009-lot-flow-and-fees.md) | Lots, gifts, fees and blocking conditions |
| [0010](0010-doxx-propagation.md) | Doxx propagation rules and confidence levels |
| [0011](0011-form-8949-box-selection.md) | Form 8949 rows and box selection |
| [0012](0012-accepted-in-process-egress-risk.md) | Accept in-process egress risk (no OS-level network sandbox) |
| [0013](0013-supply-chain-policy.md) | Supply-chain policy |
| [0014](0014-architecture-baseline.md) | Adopt the v1 architecture baseline |
| [0015](0015-local-authentication.md) | Local authentication: bootstrap file and bearer session |
| [0016](0016-default-browser.md) | Use the user's default browser; no managed profile in v1 |
| [0017](0017-no-dev-live-separation.md) | No technical separation of development and real-data use |
| [0018](0018-repository-governance.md) | Repository governance, agent rules and document precedence |
| [0019](0019-public-descriptor-import.md) | Import addresses and public descriptors only |
| [0020](0020-review-panel.md) | Automated review panel and tripwire; the human merges |
| [0021](0021-standing-method-automatic.md) | Accounts that use their standing method need no lot picking |
| [0022](0022-install-guard-is-hygiene.md) | The install-command guard is hygiene against accidental installs |
| [0023](0023-review-panel-refinements.md) | Review panel refinements: severity rules, round-limit walkthrough, no data-volume checks, no symlinks |
| [0024](0024-native-code-dev-tools.md) | Prebuilt native-code wheels for four development tools |
| [0025](0025-coverage-startup-hook.md) | Allow coverage.py's own start-up hook, pinned by content |
| [0026](0026-workflow-linters.md) | actionlint and zizmor as pinned toolchain binaries, with zizmor's online audits |
| [0027](0027-osv-scanner-audit.md) | osv-scanner as the pinned vulnerability audit, with a dev-time flow to OSV |
| [0028](0028-web-stack-fastapi-uvicorn.md) | FastAPI and uvicorn as the runtime web stack, with pydantic-core's native wheels |
| [0029](0029-minimum-core-31-1.md) | Minimum Bitcoin Core version 31.1 |
| [0030](0030-cruise-mode.md) | Cruise mode: a lighter review panel, gated automatic merges and milestone loops |

Template: [0000-template.md](0000-template.md).
