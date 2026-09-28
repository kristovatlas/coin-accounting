# Coin Accounting — v1 Plan (Bitcoin Core)

## Context
Greenfield project. Goal: let a Bitcoin holder build a historical record of coin ownership with little effort, then use it for (a) US cost-basis / capital-gains and income reporting and (b) privacy decisions ("which coins are already linked to which identity-knowing party").
Hard constraints:
- Chain data comes **only** from the user's own Bitcoin Core node, over **loopback** JSON-RPC. No block explorers or Electrum servers. A remote node is reachable only through a user-managed SSH tunnel that terminates on loopback.
- The only outbound network use is a bulk, date-independent download of fiat price history, optionally over Tor/SOCKS5.
- Data at rest is protected by keeping the user DB on a VeraCrypt volume. The app adds no crypto layer of its own.
- **Platforms: Linux and macOS.** Avoid OS-specific mechanisms unless both platforms are covered.
- All tax figures are computed in **USD**. Other fiat currencies are for display only.
- Other chains come later; the lot engine is kept chain-agnostic where it's cheap to do so.

Decisions made with the user:
- a local web app
- chain data from Core's own indexes (`txindex`, `blockfilterindex` via `scanblocks`/`getdescriptoractivity`, `txospenderindex` via `gettxspendingprevout`); **no app-side chain index**
- bulk price download
- per-account basis with Specific ID recorded in time
- doxx propagation for any identity-knowing entity
- descriptor import in v1
- in-process egress accepted as a risk (no OS sandbox)
- all three report types, plus an income report

## Architecture
```
Browser UI (Vite + TypeScript + React + Cytoscape.js; all assets bundled, no CDN)
        │  HTTP on 127.0.0.1 only (Host check, one-time launch token → cookie, CSRF header)
FastAPI backend (Python 3.12, uv-managed)
   ├── rpc.py ── Bitcoin Core JSON-RPC (dedicated rpcauth user, server-side rpcwhitelist, loopback only)
   │              node provides txindex + blockfilterindex + txospenderindex (Core ≥ 31.0)
   └── user DB (SQLite, sensitive, on VeraCrypt volume; file mode 0600), including the chain-data cache
       (nothing the app writes lives on plain disk)
```

### 1. Chain data from Bitcoin Core (`backend/coinacct/chain/`)
The app keeps **no chain index of its own**. Bitcoin Core's built-in indexes answer every chain question on demand, and the app caches the answers it needs in the user DB. Maintaining the node (initial sync, pruning, restores, resyncs) is the user's responsibility; the app only checks its settings. Details were verified against BIP158 and Core's source (`rpc/blockchain.cpp`, `rpc/mempool.cpp`, `index/*`) on 2026-09-27, and reviewed by Opus 5.5 and Codex.

- **Node requirements, checked at startup** (the app refuses to run with a clear message if any fails):
  - **Bitcoin Core ≥ 31.0**, the first release with `-txospenderindex`. The user accepts any minimum version (2026-09-27).
  - `txindex=1`, `blockfilterindex=1` and `txospenderindex=1`. Each must be reported by `getindexinfo` with `synced: true` **and** `best_block_height == getblockcount()` (retried briefly, since indexes follow the tip asynchronously).
  - An **unpruned** node (`getblockchaininfo.pruned == false`).
  - A matching chain.
  - Disk cost on the node: `txindex` (tens of GB), the filter index (~4–5 GiB in 2019, larger now) and the spender index. M1 measures them.
- **RPC access:**
  - A dedicated `rpcauth` user, restricted in Core by `rpcwhitelist=<user>:<methods>` to read-only methods. The app keeps its own allowlist too, as a second layer. The docs recommend a **generic username** (e.g. `ro-client`): see the canary note below.
  - Loopback only.
  - **Canary:** at startup the app calls a harmless method that is *not* whitelisted (`uptime`). If the call succeeds, the server-side whitelist is missing, and the app refuses to run. Core logs every refused call to `debug.log` as a warning naming the RPC user, so this leaves a usage trace on the node (THREAT_MODEL T-209).
  - JSON-RPC request `id`s are plain counters, since `debug=rpc` logs them.
  - The app never uses the node wallet or the REST interface.
  - Parallel calls are capped below `rpcthreads`/`rpcworkqueue` (16/64 by default).
- **How each question is answered:**

  | Question | Core RPC |
  |---|---|
  | Transaction details, and backward expansion (the tx that created an input) | `getrawtransaction <txid> 2 [<blockhash>]`: decoded, with prevout values and scripts. Prevouts are only included for **confirmed** txs; for an unconfirmed tx, each parent is fetched separately. The genesis coinbase can't be fetched. **BIP30:** `txindex` keeps only the later of the two duplicate coinbase txids, so every cached tx is keyed by (txid, block hash) and fetched with the block hash |
  | Forward expansion: which tx spent an output? | **`gettxspendingprevout [{txid, vout}]`**. It checks the mempool, then `txospenderindex`, and returns the spending txid and block hash. It waits for the index to catch up with the chain before answering, and errors loudly if the index is missing. First the output is classified: OP_RETURN / provably unspendable outputs are shown as terminal "unspendable" (they never enter the UTXO set or the filters) |
  | Is this output spent? | Same call; a spender that is in the mempool is marked *unconfirmed* |
  | Address/script history: every tx paying to or spending from a script | `scanblocks` → candidate block hashes → `getdescriptoractivity [blocks] [descriptors] false` → exact `receive` events (txid, vout, amount) and `spend` events (spend_txid, spend_vin, prevout txid/vout, amount). Core matches per block, so filter false positives produce no events. `include_mempool` is **always false** for block batches; the mempool is handled separately (below). See the scan protocol for how gaps and reorgs are handled |
  | Descriptor/xpub discovery | Descriptors are passed as `{desc, range}` objects (a bare ranged string silently means 0–1000). The window size comes from the M1 measurements. `scanblocks` returns only block hashes, not which derived script matched, so hits come from `getdescriptoractivity` and are mapped to derivation indexes through an app-side script→index table built with `deriveaddresses`. The window is extended (a further scan) only when the highest used index is within the gap limit of its edge. With an unknown script type, the candidate single-sig types (`pkh`, `sh(wpkh)`, `wpkh`, `tr`) go in one scan. `combo()` covers single keys but not taproot. **Multisig requires the full descriptor** |
  | Early/unusual scripts | P2PK via `pk()`/`combo()`; any script via `raw(<hex>)`. Filters contain exact script bytes, so each script type needs its own descriptor |
  | Unconfirmed activity | One separate, ephemeral mempool pass (`getdescriptoractivity [] [descriptors] true` and `gettxspendingprevout`). Its results live apart from the block-backed cache, are shown as *unconfirmed*, and are rebuilt on each refresh, so replaced (RBF), evicted and confirmed txs don't linger |
  | Chain tip, headers, confirmations | `getblockchaininfo`, `getbestblockhash`, `getblockheader`, `getblockhash` |

- **Why this is complete:**
  - A BIP158 basic filter contains, for every tx in the block, the script of each output (except OP_RETURN and empty scripts) **and the script of each output being spent** (except the coinbase). So a scan for a script finds every block where it was paid **or** spent.
  - The false-positive rate is about 1 in 785,000 per script scanned, so it grows with the number of scripts. A 1000-index window of four script types flags about 1% of blocks. Core's exact matching in `getdescriptoractivity` removes the false positives, at the cost of reading each flagged block. M1 measures this.
  - Forward expansion doesn't depend on filters at all (`txospenderindex`).
- **Scan protocol: guarding against silent gaps.** Core's `scanblocks` has a flaw: if the filter index can't return a range, that chunk (up to 10,001 blocks) is **skipped without an error**. `completed` stays true, and `to_height` equals the requested stop height by construction, so neither signals the skip. The same can happen if the index lags the tip, or if a reorg lands mid-scan. The protocol for every scan:
  1. Read the filter index's `best_block_height` (H) and the tip. The scan's stop height S is at most H **and** at most the tip minus 100. Record `getblockhash(S)`.
  2. Scan **bounded height ranges**, e.g. 50,000 blocks, sized in M1, rather than the whole chain. Core returns results only at the end of a scan, so each range is processed and committed separately. This makes progress visible and limits how much a failed scan wastes.
  3. After each range: the index height must still be ≥ S, and `getblockhash(S)` must be unchanged. Otherwise the range's results are discarded and the range is retried.
  4. **The newest ~100 blocks never go through `scanblocks`.** Their hashes are passed straight to `getdescriptoractivity`, which reads block and undo data and **errors** rather than skipping.
  5. If `getdescriptoractivity` fails with "Block is not in main chain" (a reorg between steps), the affected range is rescanned, not treated as a fatal error. Any other failure is a hard error.
  6. The remaining risk is a read error or corruption in the node's filter index, which is silently skipped. This is covered by "the node and its indexes are trusted" (THREAT_MODEL §8). We report the behaviour upstream, and adopt a fixed Core version when one exists.
- **Busy scripts:** a tagged third-party script, such as an exchange hot wallet, can have 100k+ relevant blocks. `getdescriptoractivity` has no paging, cap, progress or abort, and builds its whole result in memory. So:
  - calls are capped at a few hundred blocks each
  - each script has an activity budget: `scanblocks` gives the candidate count first, and above the threshold the app warns and asks before continuing
  - the clustering suggestions never auto-expand busy scripts
- **Scan jobs:** Core runs one `scanblocks` at a time **for all RPC users**, and a scan keeps running even if the client disconnects. So:
  - scans are queued and run in the background, with progress from `scanblocks status` over a second connection and cancellation via `scanblocks abort`
  - "Scan already in progress" means *queue busy* and is retried with backoff
  - at startup, and after any client error, the app checks `status` and aborts a scan it left running
  - client timeouts are long but finite
- **Caching and chain state:**
  - Cached chain data (decoded txs, receive/spend events, spenders) lives in the user DB on the volume, since it reveals which scripts the user cares about. Each row records the block hash and height it came from.
  - Each script or descriptor records its **coverage**: the height range scanned and the hash of the stop block.
  - Negative answers ("unspent", "no activity") are stored as **snapshots**, with the tip hash they were computed against, never as facts.
- **Reorgs and new blocks:**
  - The app keeps a persisted "last seen tip".
  - On startup and on every tip change it finds the fork point by walking `getblockheader` back while `confirmations == -1`.
  - It invalidates every cached row, coverage range and snapshot above the fork height, **at any depth**.
  - It then extends coverage through the new tip using the scan protocol.
  - Derived events that change are surfaced for review.
- **Perf check (M1):** run by the human, per the agent rules in P0.2, on a mainnet node using public scripts that are not the user's. It measures:
  - the sizes of the node's indexes
  - `scanblocks` time per range, for one script and for a ranged descriptor
  - false-positive block counts for different window sizes
  - `getdescriptoractivity` time per block
  - the behaviour of a high-volume public script

  The range size, window size and activity budget are set from these results. Any change of approach needs an ADR.

### 2. User data model (`backend/coinacct/db/`, SQLite + Alembic migrations)
Amounts are integer sats. Fiat is `Decimal`, stored as a string with a currency code. The tax ledger is USD.
- `entity`: id, name, kind (`self | exchange | employer | merchant | person | unknown`), **`knows_identity`** flag (default true for exchanges and employers), notes.
- `tax_account`: the unit for per-wallet/account basis (Rev. Proc. 2024-28).
  - kind: `self_custody | custodial`; name
  - **standing identification method** (`fifo | specific | …`): what the user or broker has on file
  - For a custodial account: the entity (exchange) and 1099-DA flags.
- `wallet_client`: software/hardware clients (`hardware | mobile | desktop | web | paper | other`). **Labels only**, many-to-many with addresses. An address can live in several clients, e.g. generated on a hardware wallet and imported into a mobile wallet.
- `address`: keyed by **scripthash**, so P2PK and bare multisig work. Fields:
  - optional address string
  - owner entity_id
  - `tax_account_id` (exactly one for owned addresses)
  - label, source, confirmed flag
- `address_client`: many-to-many address↔wallet_client.
- `descriptor`: **public-only** descriptor/xpub, gap limit, highest used index, script→index table, tax_account, client(s).
- Chain-data cache (recomputable from the node):
  - `tx_cache`: decoded txs, keyed by (txid, block hash) for BIP30
  - `activity`: receive/spend events per script
  - `spender`: outpoint → spending tx + block hash
  - `coverage`: per script/descriptor: scanned height range + stop block hash
  - `snapshot`: negative answers, with the tip hash they were computed against
  - `chain_state`: last seen tip
  - Every block-backed row records its source block hash and height.
  - Mempool results are kept separately and are ephemeral.
  - `tx.mixing` flag (auto-detected, user-overridable).
- `event`: a typed ledger entry. Types:
  - acquisitions: `buy | p2p_buy | income | gift_in | inherit | opening_allocation_2025`
  - movements: `self_transfer | deposit | withdrawal`
  - disposals: `sell | spend | gift_out`
  - `fee`

  Each event carries: UTC timestamp, sats, USD amount, valuation source, a fee role, and links to txid/outpoint or tax_account.
- `lot`: acquisition timestamp, sats, USD basis, origin event.
  - Gifts additionally store donor basis, donor date, FMV at gift, and gift date (dual basis).
  - Inheritance stores FMV at death.
- `lot_fragment`: (holder = outpoint *or* tax_account, lot_id, sats). The engine can recompute it as of any date.
- `identification`: disposal/withdrawal → lot choices, `identified_at` timestamp, method (`specific | standing_order | fifo_default`), and a `late` flag.
- `disposal_allocation`: disposal → lot_id, sats, basis, proceeds share (net of disposal costs), holding period, and the 8949 box plus the reason it was chosen.
- `doxx_tag`: outpoint or scripthash, entity_id, **confidence** (`certain | inferred`), reason (`paid_to | change_of | co_spent_with | address_reuse | cluster_backward | received_from | manual`), source txid, `manual_override`.
- `price`: UTC date, currency, price, source, method, content hash.
- `change_log`: append-only record of every edit to events, tags, identifications and overrides.
- `settings`: fee treatment, time zone, proxy, confirmation threshold, etc.

### 3. Import, discovery, clustering (`discovery.py`, `tagging.py`)
- **Import:**
  - paste or CSV a list of addresses
  - **descriptors/xpubs (v1), public material only.** A descriptor or key containing private material (xprv, WIF) is **rejected** at import and never sent to the node or stored; the user is shown how to export the public form. Descriptors are derived with Core's `deriveaddresses` (no node wallet) under a gap limit that extends as used addresses are found
  - Each import is assigned to an entity (default "me"), a tax account, and wallet client(s).
- **Discovery:** scan each imported script or descriptor with `scanblocks` (batched into as few scans as possible) to get all funding and spending txs. Those are cached, and owned UTXOs (current and historical) are derived.
- **Third-party tags** work the same way (e.g. an employer's address). Suggestion engine:
  - **Common-input heuristic:** addresses co-spent with a tagged address are suggested as the same entity. This is suppressed for txs flagged `mixing` (CoinJoin/PayJoin-like: many equal-value outputs, or set by the user).
  - A tx that pays the user from a tagged cluster produces a suggested event (e.g. `income` from Employer).
  - Suggestions are never auto-applied. The user accepts or rejects each one, rejections are remembered, and the tag records its provenance.

### 4. Graph UI (`frontend/`)
- Cytoscape.js with a DAG layout. Nodes are txs and outputs. Outputs are colored by owning entity, and badges show doxx sets, with certain and inferred links drawn differently.
- Click an output to expand backward (funding tx) or forward (spender, via one `gettxspendingprevout` call). Expansion is lazy and uses paging for large txs.
- A side panel shows address/tx details. From it the user can:
  - tag the owner entity, tax account and clients
  - set the mixing flag
  - create or confirm an event
  - set the valuation
  - see the lot fragments in the UTXO
- Other views:
  - address list, entity/account/client manager
  - suggestions inbox
  - exchange accounts and sells
  - lots/holdings as of a date
  - reports
- Wording: the doxx UI says "known links", never "private" or "clean", and keeps a persistent note about heuristics the app doesn't model.

### 5. Doxx propagation (`doxx.py`)
The doxx set of a coin is the set of **identity-knowing entities** (`knows_identity`) that can link that coin to the user. Rules are deterministic from the tagged graph, manual overrides persist, and the rules are recorded in an ADR.
1. **Paid to K:** a tx pays an output owned by K (a deposit, a purchase, a payment to an employer or KYC'd person). K sees every **owned input** and every **owned output (change)** of that tx, so all of them are doxxed to K (*certain*).
2. **Received from K:** a withdrawal, salary or other payment the user recorded as coming from K. The received outpoint and its address are doxxed to K (*certain*). This comes from the confirmed event, not from recognizing K's addresses; clustering is only supporting evidence.
3. **Address reuse:** every output ever paid to an address that holds a K-doxxed coin is doxxed to K (*certain*).
4. **Forward:** owned outputs of a tx inherit the union of its owned inputs' doxx sets (*certain*). Exception: for txs flagged `mixing`, outputs inherit only as *inferred*, and inputs are not linked to each other.
5. **Backward cluster:** owned addresses in the same common-input cluster as a doxxed address, i.e. co-spent with it in earlier non-mixing txs, are doxxed to K (*inferred*).
- Doxx sets are multi-valued ({X, Y}). Outputs to third parties are not propagated further.
- **Sell planner:** lists UTXOs by doxx set, with basis, holding period and certainty.

### 6. Prices (`prices/`)
- **USD:** bulk-download Bitstamp history and compute a **daily volume-weighted average price per UTC day**.
  - Planned source: the full bitstampUSD trade dump (bitcoincharts archive) for history, plus Bitstamp's paginated daily OHLCV for recent days.
  - Where there is no trade-level data, fall back to the daily typical price, (H+L+C)/3. The `method` column records which was used.
  - The M5 step checks that these sources are still up.
- **Other fiat (display only):** Bitstamp EUR/GBP pairs, or USD × ECB historical FX. **All supported pairs are fetched every time**, so the download doesn't reveal the user's currency or residency.
- **Requests:** made only when the user clicks refresh. They don't depend on user records (events, addresses, tags); only the start of the incremental range depends on what is already cached. An httpx client with an optional SOCKS5/Tor proxy (remote DNS) sends a common browser User-Agent and nothing else.
- **Overrides:** the user can override the valuation per event, e.g. an exchange fill price, the W-2/payroll value for salary, or a timestamped rate for income on a volatile day.

### 7. Lot / tax engine (`tax/`)
A pure, deterministic function of events, recomputed on every change. It can compute state as of any date. Tax rules are **versioned by tax year**, with references to IRS forms, instructions and guidance.
- **Acquisitions create lots:**

  | Event | Basis | Holding period starts |
  |---|---|---|
  | `buy` (exchange) | cost + acquisition fees | date of purchase |
  | `p2p_buy` | fiat actually paid + fees | date of purchase |
  | `income` / mining | USD FMV at receipt time, also recorded as ordinary income | receipt date |
  | `gift_in` | dual basis: donor basis for gains; FMV at the gift, if lower, for losses; no gain/loss between the two; donor basis unknown → zero, flagged | donor's date (tacked) for gain basis; the gift date when loss basis applies |
  | `inherit` | FMV at death | always long-term |
  | `opening_allocation_2025` | the user's documented allocation of unused pre-2025 basis to each tax account | carried from the allocated lots; overrides derived fragments for that account |

- **Movements never create lots.**
  - Self-transfers between owned UTXOs move fragments pro-rata by sats, and holding periods carry over.
  - A `deposit` moves fragments into the custodial tax account.
  - A `withdrawal` moves fragments from the custodial account to the received UTXO. The fragments are chosen by the identification rules below.
  - A lot is created on withdrawal only when the account has no known fragments (buys were never entered). The user must then supply the original date and basis; otherwise it is *unknown basis*.
- **Disposals** (`sell`, `spend`, `gift_out`) draw only from lots in the **same tax account** (per-account basis).
- **Identification timing:**
  - Specific ID counts only if recorded **no later than the sale**. For exchanges, it goes to the broker; through 12/31/2026, the taxpayer's own books and records are also accepted (Notice 2025-7, extended by Notice 2026-20).
  - `identified_at` is stored. A pick made after the sale is flagged `late`, and the account's standing order, or else FIFO, applies instead.
  - For 2027+ sales the UI warns that the identification must be communicated to the broker.
  - For on-chain disposals, the spent UTXO is itself the identification. Within that UTXO, fragments are consumed by the account's standing method.
- **Fees by role:**
  - acquisition fees add to basis
  - disposal fees reduce proceeds; proceeds are net of costs, matching 1099-DA
  - network fees on self-transfers/deposits: the default carries the fee's basis over to the remaining sats; a setting instead treats it as a small disposal
  - network fees on `spend`: they reduce proceeds
  - BTC withdrawal fees charged by an exchange: handled as a small disposal (default)
  - A tx mixing owned and third-party outputs splits the fee by role
- **Blocking conditions:** unknown basis, unconfirmed txs (below the confirmation threshold), or late identifications block report generation. Each needs an explicit user resolution, which is recorded in the change log.
- **Dates:** events use UTC timestamps. The tax date is converted to the user's configured time zone. Block timestamps can be off by about ±2h, so the user can override them with exchange-recorded times.
- **Short/long-term:** held for more than one year counts as long-term.
- **Out of scope for v1** (documented, and the UI warns if they seem to apply): lost/stolen coins, forks/airdrops, state taxes. §1091 wash-sale rules do not apply to BTC (not a security); a future toggle is noted.

### 8. Reports (`tax/reports.py`)
- **Form 8949 CSV:** one row per disposal-lot allocation. The box is chosen **per disposal** from (tax year, disposal channel, 1099-DA received?, basis reported?), and the user can override it with the reason recorded:
  - ≤2024: A/B/C (short-term) and D/E/F (long-term). Digital assets without a 1099-B go in C/F.
  - ≥2025: digital assets go in **G/H/I** (short-term) and **J/K/L** (long-term). C/F may not be used for them.
    - on-chain disposals (`spend`, `gift_out`, fee disposals) → I/L
    - 2025 exchange sells default to H/K, since brokers report proceeds but not basis
    - exchange sells of 2026+ acquisitions default to G/J when the broker reports basis
- **Ordinary income summary** per year: income events with their USD FMV and source.
- **Year summary + holdings:** realized short/long-term gains per year; lots held as of any date, with basis, holding period and unrealized gain.
- **Audit trail:** for each lot, the chain from acquisition event → every tx hop (txids) → identification → disposal. Exported as CSV and JSON.
- Every report embeds the app version, rule-set version, settings, and a hash of its inputs.
- **CSV exports are type-aware:** numeric columns are written as numbers, and only free-text columns are escaped against formula injection.

## Repo layout
```
backend/coinacct/{config.py, rpc.py, app.py, chain/{node_checks.py, scans.py, cache.py, reorg.py},
                  db/{models.py, migrations/}, discovery.py, tagging.py, doxx.py,
                  prices/{bitstamp.py, fx.py}, tax/{engine.py, rules/, reports.py}, api/*.py}
backend/tests/{unit/, regtest/}
frontend/{src/{views/, graph/, api.ts}, vite.config.ts}
docs/{THREAT_MODEL.md, ENGINEERING.md, architecture.md, DEPENDENCIES.md, adr/}
AGENTS.md (vendor-neutral agent instructions; points to the docs above as binding rules)
CLAUDE.md (one line: `@AGENTS.md`, so Claude Code loads the same rules; no content of its own)
pyproject.toml, uv.lock, pnpm-workspace.yaml/.npmrc (cooldown, no scripts), README.md
```

## Phase 0 — Gating documents (no application code until the human approves all four)
Order: design (this plan) → **P0.1 threat model** → **P0.2 engineering practices** → **P0.3 architecture diagram** → **P0.4 ADRs**. Each is a draft PR the human reviews. After approval, `AGENTS.md` (the cross-tool standard) points to all four as binding, so every agent session honors them. `CLAUDE.md` only imports `AGENTS.md`, so there is a single source of truth.

### P0.1 `docs/THREAT_MODEL.md` (living document)
- **Scope:** assets, adversaries, trust boundaries drawn from a data-flow diagram, STRIDE per boundary plus privacy and tax-integrity threats, an exhaustive list of network flows, accepted risks.
- **Per-threat status:** `Planned | Implemented | Verified | Documented | Accepted | N/A`.
  - *Verified* requires a test that fails if the mitigation is removed.
  - *Documented* is the ceiling for procedural or documentation-only mitigations.
- The threat model is updated in the same PR as any change that affects a threat.

### P0.2 `docs/ENGINEERING.md` (practices; agreed before any code)
The practices live in [`docs/ENGINEERING.md`](docs/ENGINEERING.md), and that file is authoritative. In summary:
- **Supply chain:**
  - pnpm 12 and uv, both with 7-day cooldowns
  - no build or lifecycle scripts; wheels only
  - no auto-installs or auto-downloads
  - every install through Socket Firewall via `make` targets with no fallback
  - dependencies are **resolved, vetted and approved before anything is installed**
  - a lockfile policy check
  - verified toolchain and test downloads
  - audits on every PR
- **CI:** hardened Actions, no secrets, a reproducible frontend build, Linux + macOS.
- **Testing:**
  - E2E first, against the production build under the real CSP
  - a test-time socket guard
  - coverage floors: 85% backend, 95% for `tax/`/`doxx.py`/`chain/` from unit + integration tests, 70% frontend
  - per-PR mutation testing
  - anti-test-slop rules and audits
- **Records:** ADRs are immutable after acceptance, and the architecture diagram is hash-locked to an ADR.
- **AI agents:**
  - no real data
  - they propose dependencies but never approve them
  - only the human merges (procedural)
- **Commit signing is not required.**

### P0.3 `docs/architecture.md` — architecture diagram (human-reviewed, stays binding)
- A Mermaid diagram (renders on GitHub and as text in the repo) of components, trust boundaries, data stores (all on the VeraCrypt volume), and every network flow.
- Also a data-flow diagram for "import → discover → tag → lot → report".
- A change is allowed only through an ADR. A CI check keeps a hash of the approved diagram, and changing it without a new ADR fails.

### P0.4 Initial ADRs
Seed ADRs record the decisions already made:
- local web app
- supported platforms (Linux + macOS)
- node access: loopback JSON-RPC, rpcauth + rpcwhitelist, unpruned node, minimum Core version
- chain data via Core's `txindex` + `blockfilterindex`/`scanblocks`, no app-side index
- bulk, date-independent price download
- per-account basis, identification timing, the 2025 transition
- lot flow and fee treatment by role
- doxx propagation rules and confidence levels
- 8949 box selection rules per tax year
- storage: everything the app writes lives on the VeraCrypt volume
- accepted egress risk
- pnpm/uv supply-chain policy

## Milestones
Every milestone ends by updating the THREAT_MODEL status, any ADRs, and the diagram if needed, and it passes the coverage floors and E2E flow on Linux and macOS.

1. **M0 skeleton:**
   - config
   - RPC client with node checks (pruned, chain, version, whitelist canary)
   - storage checks: VeraCrypt detection on Linux and macOS, file modes
   - FastAPI app with Host check, launch token, CSRF and CSP
   - a regtest harness that uses a verified `bitcoind` download
2. **M1 chain access:** node checks (including index sync and the canary), the tx fetch layer, spender lookups, the scan protocol (bounded ranges, gap guards, tip handling), the scan job queue (status/abort, leftover-scan cleanup), busy-script budgets, the chain-data cache with coverage and snapshots, fork-point reorg handling, the mempool pass, and the mainnet perf check.
3. **M2 user DB + import + discovery:**
   - entities, tax accounts, clients
   - address and **descriptor/xpub** import
   - address/UTXO/tx history views
4. **M3 graph UI:** backward/forward expansion, the tagging side panel, the mixing flag.
5. **M4 clustering suggestions + doxx propagation:** certain and inferred links, plus the sell planner view.
6. **M5 prices:** bulk USD VWAP, display FX for all pairs, proxy support.
7. **M6 lot engine:**
   - events, per-account basis
   - identification timing
   - the 2025 opening allocation
   - fees by role
   - gifts and inheritance
   - blocking conditions
8. **M7 reports:** 8949 CSV (box selection per tax year), income summary, year summary/holdings, audit trail.
9. Later:
   - exchange CSV import
   - in-app lock (see threat model open questions)
   - a second price source for cross-checking (needs an ADR, since it adds an outbound flow)
   - lost/stolen and fork/airdrop events
   - other chains (account-based FIFO/LIFO) once their privacy model is settled

## Verification
- **Unit tests (pytest):**
  - scan protocol: range sizing, stop height below the index height and the tip−100, stop-hash checks, the tip-window path through `getdescriptoractivity`
  - descriptor handling: `{desc, range}` objects, script→index mapping, gap extension, rejecting private keys
  - output classification (unspendable/OP_RETURN) and BIP30 (txid, block hash) keys
  - cache invalidation on reorg
  - doxx rules on hand-built graphs, covering every rule and the mixing exception
  - the lot engine against hand-worked tax scenarios:
    - pro-rata moves
    - withdrawals moving lots
    - all three gift outcomes
    - late identification
    - the 2025 opening allocation
    - fee roles
    - the 1-year boundary
  - 8949 box selection per tax year
- **Regtest integration:** script `bitcoind -regtest` to:
  - mine blocks
  - derive descriptor addresses
  - make self-transfers
  - pay an "exchange" address and reuse an address
  - make a CoinJoin-like tx
  - spend change
  - force reorgs, including one during a scan and one while the app is stopped (`invalidateblock` in the test harness, never in the app)
  - make the filter index lag the tip
  - add mempool txs that are then replaced (RBF) or evicted

  Then assert that discovery, forward/backward expansion, cache invalidation, doxx tags and lot flow all match expectations.
- **End-to-end** (Playwright, Linux + macOS): run the backend + UI against regtest and drive the whole flow: import descriptor → expand graph → tag exchange → record sell with timely identification → export 8949. The socket guard log confirms that no unexpected connections were made.
- **Mainnet smoke test:** run **by the human** on their node with a small address set once M1–M3 land. Agents do not take part.
