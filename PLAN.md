# Coin Accounting — v1 Plan (Bitcoin Core)

## Context
Greenfield project (empty repo). Goal: let a Bitcoin holder build a historical record of coin ownership with little effort, then use it for (a) US cost-basis / capital-gains reporting and (b) privacy decisions ("which coins are already doxxed to which exchange").
Hard constraints:
- Chain data comes **only** from the user's local Bitcoin Core node. No block explorers or Electrum servers.
- The only outbound network use is a bulk download of fiat price history, optionally over Tor/SOCKS5. It never sends per-date or per-tx queries.
- Data at rest is protected by keeping the user DB on a VeraCrypt volume. The app adds no crypto layer of its own.
- USD first, but the design supports multiple fiat currencies. Other chains come later; the lot engine is kept chain-agnostic where it's cheap to do so.

Decisions made with the user: a local web app, Core plus our own full-chain index, bulk price download, Specific ID for exchange lots, doxx propagation via forward + co-spend, all event types, and all three report types.

## Architecture
```
Browser UI (Vite + TypeScript + React + Cytoscape.js; all assets bundled, no CDN)
        │  HTTP on 127.0.0.1 only (Host-header check + CSRF token against DNS rebinding)
FastAPI backend (Python 3.12, uv-managed)
   ├── rpc.py ── Bitcoin Core JSON-RPC only (cookie auth, localhost)
   ├── chain index (LMDB, public data, large, path configurable, need not be on VeraCrypt)
   └── user DB (SQLite, sensitive, lives on VeraCrypt volume; path set at launch)
```

### 1. Node + chain index (`backend/coinacct/index/`)
- Required node setting: `txindex=1` (checked via `getindexinfo`). JSON-RPC only: raw blocks come from `getblock <hash> 0`. The REST interface (`rest=1`) is not used, because it is unauthenticated and its only gain is smaller transfers. If the M1 benchmark shows block transfer is a bottleneck, the alternative is reading `blk*.dat` directly, and that needs an ADR.
- Own index, full chain from genesis, electrs-style compact keys in LMDB:
  - `S | scripthash[8] | height[4] | txid[8]` → funding occurrence (address history). Keying on the sha256 of scriptPubKey covers every script type.
  - `O | txid[8] | vout[4]` → spending `txid[8] | height[4]` (enables "expand forward").
  - `T | txid[8]` → height (disambiguates prefixes).
  - Prefix collisions are resolved by fetching the full tx via `getrawtransaction` (txindex) and verifying.
- Sync: parse raw blocks in parallel worker processes, write in height batches, and keep a tip marker. Incremental sync on startup and on a poll. Reorg handling keeps the last ~100 block undo entries and rolls back on hash mismatch.
- **Perf gate:** benchmark 10k mainnet blocks in M1, timing RPC fetch, parsing and LMDB writes separately. If the projected full sync is over ~24h, move the parse/insert hot loop to a small Rust extension (PyO3/maturin) behind the same interface.
- The expected size is ~50–100 GB. The UI shows sync progress, and the app is usable for heights already indexed.

### 2. User data model (`backend/coinacct/db/`, SQLite + Alembic migrations)
Amounts are integer sats. Fiat is stored as a decimal string with a currency code.
- `entity`: id, name, kind (`self | exchange | employer | merchant | person | unknown`), notes. It covers both the user and third parties.
- `wallet`: id, name, kind (`hardware | mobile | desktop | web | custodial | paper | other`), notes.
- `address`: address, scripthash, entity_id (owner), label, source (imported / suggested / manual), confirmed flag.
- `address_wallet`: many-to-many. One address can belong to several wallet clients, e.g. generated on a hardware wallet and imported into a mobile wallet.
- `tx_cache`, `utxo_cache`: decoded tx/outputs pulled from the node for relevant txs (recomputable).
- `exchange_account`: entity_id, name, fiat currency.
- `event`: typed ledger entry (`buy | income | p2p_buy | gift_in | self_transfer | deposit | withdrawal | sell | spend | gift_out | fee`) with timestamp, sats, fiat value, price source, and links to txid/outpoint or exchange_account.
- `lot`: acquisition date, sats, basis (fiat plus currency), origin event, and for gifts-in the donor's basis/date (carry-over basis).
- `lot_fragment`: (holder = outpoint *or* exchange_account, lot_id, sats). It records which lots are currently inside each UTXO or exchange account.
- `disposal_allocation`: disposal event → lot_id, sats, basis used, proceeds share.
- `doxx_tag`: outpoint, entity_id, reason (`paid_to | change_of | co_spent_with | withdrawn_from | manual`), source txid, `manual_override` flag.
- `price`: date, currency, price, source, method.
- `settings`: fee treatment, default allocation method, proxy, etc.

### 3. Address import, discovery, clustering (`discovery.py`, `tagging.py`)
- Import: paste or CSV a list of addresses, assigning an entity (default "me") and wallet(s). Later: descriptor/xpub import with gap-limit derivation.
- Discovery: look up each address's scripthash in the index to get all funding/spending txs. Those are cached, and owned UTXOs (current and historical) are derived.
- Third-party tags work the same way (e.g. an employer's address). Suggestion engine:
  - **Common-input heuristic:** addresses co-spent with a tagged address are suggested as the same entity. This is how one employer address expands into the employer's cluster.
  - Any tx that pays the user from a tagged cluster produces a suggested event (e.g. `income` from Employer), which the user confirms.
  - Suggestions are never auto-applied. The user accepts or rejects each one, and rejections are remembered.

### 4. Graph UI (`frontend/`)
- Cytoscape.js with a DAG layout. Nodes are txs and outputs. Outputs are colored by owning entity, and doxx badges come from `doxx_tag`.
- Click an output to expand backward (funding tx) or forward (spender, via the `O` index). Expansion is lazy and uses paging for large txs.
- A side panel shows address/tx details. From it the user can tag the owner entity and wallet, create or confirm an event, set the acquisition date/price, and see which lot fragments sit in the UTXO.
- Other views: address list, entity/wallet manager, suggestions inbox, exchange accounts and sells, lots/holdings, reports.

### 5. Doxx propagation (`doxx.py`)
Rules are computed deterministically from the tagged graph, and manual overrides persist.
- A tx pays an output owned by exchange X (a deposit) → every **owned** output of that tx (change) is doxxed to X.
- Every owned input co-spent in that tx carries its doxx set onward. Any owned output of a tx whose inputs include a doxxed coin inherits the union of the input doxx sets (co-spend + forward).
- A withdrawal from exchange X (a tx funded by an X-tagged address paying the user) → that output is doxxed to X.
- It is multi-valued: a coin can be doxxed to {X, Y}. Outputs to other parties are not propagated further.
- UI: filter/colour by doxx set. The "sell planner" view lists UTXOs already doxxed to X with their basis and holding period.

### 6. Prices (`prices/`)
- USD (default): bulk-download Bitstamp history and compute a **daily volume-weighted average price**. Planned source: the full bitstampUSD trade dump (bitcoincharts archive) for history plus Bitstamp's paginated daily OHLCV endpoint for recent days, fetched in one sweep over the whole range. Where no trade-level data exists, fall back to the daily typical price. The `method` column records this per row. The M5 step checks that these sources are still up.
- Other fiat: Bitstamp EUR/GBP pairs where they exist, otherwise USD × ECB historical FX (single bulk zip).
- It is refreshed on demand. An httpx client with an optional SOCKS5/Tor proxy setting sends no other requests.
- The user can override the price per event (e.g. the actual exchange fill price for a buy).

### 7. Lot / tax engine (`tax/`)
- **Acquisition** events create lots:
  - buy / withdrawal from an exchange: basis is the exchange lot's basis
  - income / P2P / mining: basis is FMV on the receipt date
  - gift_in: the donor's basis/date, with FMV kept for the loss rule
- **On-chain spends of owned UTXOs:** input lot fragments flow to outputs pro-rata by sats (configurable to FIFO-by-lot-date).
  - Owned outputs: fragments move and holding periods carry over (self-transfer).
  - Deposit to an exchange account: fragments move into that `exchange_account`.
  - Other parties: a disposal (`spend` at FMV, `gift_out` with no gain).
- **Network fees:** the default is to fold the fee's basis into the remaining sats on self-transfers and deposits, i.e. a non-taxable transfer cost. A setting instead treats the fee as a small disposal at FMV.
- **Exchange sells:** the user enters date, sats sold, gross proceeds and fee. The UI proposes a FIFO pick from that account's fragments, which the user can edit (Specific ID, per-wallet/account, in line with Rev. Proc. 2024-28 from 2025). The result is written to `disposal_allocation`.
- Short-/long-term split (more than one year), gains in the configured fiat, and full recomputation from events whenever anything changes. The engine is a pure function, so it is easy to test.

### 8. Reports (`tax/reports.py`)
- **Form 8949 CSV:** one row per disposal-lot allocation, with description, dates, proceeds, basis, and gain. The box defaults to C/F for tax years before 2025 and G–L for 2025 onward. Box choice depends on whether a 1099-DA reported basis, so it can be set per exchange account.
- **Year summary + holdings:** realized short/long-term gains per year, plus current lots with basis, holding period, and unrealized gain at a chosen price.
- **Audit trail:** for each lot, the chain from acquisition event → every tx hop (txids) → disposal. Exported as CSV and JSON.

## Repo layout
```
backend/coinacct/{config.py, rpc.py, app.py, index/{parse.py, store.py, sync.py},
                  db/{models.py, migrations/}, discovery.py, tagging.py, doxx.py,
                  prices/{bitstamp.py, fx.py}, tax/{engine.py, reports.py}, api/*.py}
backend/tests/{unit/, regtest/}
frontend/{src/{views/, graph/, api.ts}, vite.config.ts}
docs/{THREAT_MODEL.md, ENGINEERING.md, architecture.md, DEPENDENCIES.md, adr/}
AGENTS.md (vendor-neutral agent instructions; points to the docs above as binding rules)
CLAUDE.md (one line: `@AGENTS.md`, so Claude Code loads the same rules; no content of its own)
pyproject.toml, uv.lock, pnpm-workspace.yaml/.npmrc (cooldown, no scripts), README.md
```

## Phase 0 — Gating documents (no application code until the human approves all four)
Order: design (this plan) → **P0.1 threat model** → **P0.2 engineering practices** → **P0.3 architecture diagram** → **P0.4 ADRs**. Each is a PR/commit the human reviews. After approval, `AGENTS.md` (the cross-tool standard) points to all four as binding, so every agent session honors them. `CLAUDE.md` only imports `AGENTS.md`, so there is a single source of truth.

### P0.1 `docs/THREAT_MODEL.md` (living document)
- **Scope and assets:**
  - ownership graph and address↔entity tags (the most sensitive data)
  - lot/tax data
  - doxx map
  - node RPC credentials
  - price-fetch metadata
- **Adversaries:**
  - device thief / cold-disk attacker
  - malicious local process or browser tab (DNS rebinding, CSRF against localhost)
  - network observer (price download, Tor/proxy leaks)
  - compromised dependency or build tool (supply chain)
  - malicious peer data via the node (crafted txs/scripts hitting the parser)
  - the user's future self (wrong tax figures = integrity threat)
- **Method:** trust boundaries drawn from the architecture diagram, STRIDE per boundary, and data-flow list of every outbound connection (allowed: node RPC, price bulk download; nothing else).
- **Per-threat status table:** `Planned | Implemented | Verified-by-test | Accepted-risk`, with a link to the mitigation code or test. It starts all "Planned" and is updated in the same commit as any change that affects a threat.
- **Notable items:**
  - Plaintext leakage outside VeraCrypt (logs, temp files, browser cache/history, SQLite WAL/journal location, crash dumps, Python `__pycache__` of user data, LMDB index revealing *which* scripthashes were queried; the index itself is public).
  - Localhost binding, Host-header allowlist, CSRF tokens, no CORS.
  - The price fetch never includes dates of interest.
  - Parser hardening against malformed blocks.
  - Integrity: recompute-from-events, and reports carry the input hash.
  - Clipboard and screen exposure.

### P0.2 `docs/ENGINEERING.md` (practices; agreed before any code)
- **Supply chain (JS):**
  - pnpm with `minimumReleaseAge: 10080` (7-day cooldown on any new version)
  - lifecycle/postinstall scripts blocked (pnpm 10 default; empty `onlyBuiltDependencies`)
  - `--frozen-lockfile` in CI
  - no CDN assets at runtime
- **Supply chain (Python):**
  - `uv` with a lockfile containing hashes, `uv sync --locked`
  - a 7-day cooldown via `exclude-newer`, kept rolling by a checked-in script (verify uv's relative-duration support at setup)
  - sdists only when no wheel exists, reviewed by hand
- **Dependency vetting:**
  - Every new dependency, direct or transitive, is scanned with Socket.dev (Socket CLI / Socket Firewall `sfw` wrapping installs) **before it is installed or executed**.
  - The dependency is recorded in `docs/DEPENDENCIES.md` with a justification.
  - The rule is to prefer stdlib or a small hand-written module over adding a dependency.
- **CI:**
  - GitHub Actions pinned by commit SHA, minimal token permissions
  - `pip-audit`/`pnpm audit` + Socket on each PR
  - reproducible build of the frontend bundle
- **Testing:**
  - The strategy leans on E2E: regtest `bitcoind` + backend + Playwright UI flows are the primary evidence a feature works, and unit tests back up the parser, doxx and tax engines.
  - Coverage floors, enforced in CI: overall ≥85% line+branch, and ≥95% for `tax/`, `doxx.py`, `index/parse.py`. These are floors, not targets.
  - **Anti-test-slop:**
    - A review checklist bans tests that only assert mocks, tautologies, snapshot-everything tests, and tests changed to match buggy output.
    - Tax tests use hand-worked expected values, derived independently of the code.
    - Mutation testing (`mutmut`) on `tax/` and `doxx.py` runs on a schedule, with a surviving-mutant budget.
    - A periodic "test audit" task reviews the suite and deletes or strengthens weak tests.
- **Design records:**
  - ADRs in `docs/adr/NNNN-title.md` (MADR format) for every significant or irreversible choice. Changing a decision means writing a superseding ADR, not editing history.
  - Architecture diagram changes need an ADR plus human review.
- **Code:**
  - typed Python (mypy strict), TypeScript strict, ruff/eslint, integer sats, `Decimal` for fiat
  - no network calls outside `rpc.py` and `prices/`, enforced by a test that monkeypatches sockets
- **Process:**
  - small commits
  - each PR updates the threat model status and any affected ADR/diagram
  - Definition of Done includes E2E coverage of the feature

### P0.3 `docs/architecture.md` — architecture diagram (human-reviewed, stays binding)
- A Mermaid diagram (renders on GitHub and as text in the repo) of components, trust boundaries, data stores (VeraCrypt vs non-VeraCrypt), and every network flow.
- Also a data-flow diagram for "import address → discover → tag → lot → report".
- A change is allowed only through an ADR. A CI check keeps a hash of the approved diagram, and changing it without a new ADR fails.

### P0.4 Initial ADRs
Seed ADRs record the decisions already made:
- local web app
- Core + own LMDB full-chain index
- bulk price download with no per-date queries
- Specific ID per exchange account
- doxx propagation rules
- pro-rata lot flow and fee treatment
- storage split (user DB on VeraCrypt, public index anywhere)
- pnpm/uv supply-chain policy

## Milestones
Every milestone ends by updating the THREAT_MODEL status, any ADRs, and the diagram if needed, and it passes the coverage floors and E2E flow.

1. **M0 skeleton:** config, RPC client with node checks (txindex, chain), and a FastAPI app with localhost/Host/CSRF protection. Bitcoin Core must be installed locally for regtest tests, since there is no `bitcoind` on this box yet.
2. **M1 chain index:** parser, LMDB store, sync/reorg, and the perf benchmark gate.
3. **M2 user DB + import + discovery:** entities, wallets, many-to-many address↔wallet, and the address/UTXO/tx history views.
4. **M3 graph UI:** backward/forward expansion and the tagging side panel.
5. **M4 clustering suggestions + doxx propagation**, plus the sell planner view.
6. **M5 prices:** bulk USD VWAP, FX for other fiats, and proxy support.
7. **M6 lot engine:** events, exchange accounts, Specific-ID sells, and fee handling.
8. **M7 reports:** 8949 CSV, year summary/holdings, audit trail.
9. Later: descriptor/xpub import, exchange CSV import, other chains (account-based FIFO/LIFO) once their privacy model is settled.

## Verification
- **Unit tests (pytest):**
  - block/tx parser against known mainnet raw blocks
  - LMDB key encoding and collision resolution
  - doxx rules on hand-built graphs
  - lot engine against hand-worked tax scenarios: pro-rata splits, fee options, gifts, Specific-ID sells, and the 1-year boundary
- **Regtest integration:** script `bitcoind -regtest` to mine blocks, make self-transfers, pay an "exchange" address, spend change, and force a reorg. Then assert that the index, discovery, doxx tags and lot flow all match expectations.
- **End-to-end:** run the backend + UI against regtest and drive the whole flow: import addresses → expand graph → tag exchange → record sell → export 8949. Confirm that the only non-node outbound traffic is the price download (e.g. with a proxy log or `ss`).
- **Mainnet smoke test:** run on the user's node with a small address set once M1–M3 land.
