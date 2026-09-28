# Architecture Decision Records

MADR-format records of significant decisions (see [ADR 0001](0001-record-decisions-with-adrs.md) and ENGINEERING §4). **Status is in each file's front matter. An ADR is accepted when the human merges the PR that contains it.**

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-decisions-with-adrs.md) | Record significant decisions as ADRs | proposed |
| [0002](0002-local-web-app.md) | Local web app (FastAPI + React SPA on loopback) | proposed |
| [0003](0003-supported-platforms.md) | Supported platforms are Linux and macOS | proposed |
| [0004](0004-node-access.md) | Node access via loopback JSON-RPC with a whitelisted rpcauth user | proposed |
| [0005](0005-chain-data-via-core-indexes.md) | Chain data from Core's own indexes; no app-side chain index | proposed |
| [0006](0006-storage-on-veracrypt-volume.md) | Everything the app writes lives on the VeraCrypt volume | proposed |
| [0007](0007-price-data-bulk-download.md) | Fiat prices by bulk, date-independent download | proposed |
| [0008](0008-per-account-basis-and-identification.md) | Per-account basis, timely identification, the 2025 transition | proposed |
| [0009](0009-lot-flow-and-fees.md) | Lot flow across on-chain hops, and fees by role | proposed |
| [0010](0010-doxx-propagation.md) | Doxx propagation rules and confidence levels | proposed |
| [0011](0011-form-8949-box-selection.md) | Form 8949 box selection per disposal and tax year | proposed |
| [0012](0012-accepted-in-process-egress-risk.md) | Accept in-process egress risk | proposed |
| [0013](0013-supply-chain-policy.md) | Supply-chain policy | proposed |
| [0014](0014-architecture-baseline.md) | Adopt the v1 architecture baseline | proposed |

Template: [0000-template.md](0000-template.md).
