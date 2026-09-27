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
- chain data from Core's own indexes (`txindex`, `blockfilterindex` + `scanblocks`); **no app-side chain index**
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
   │              node provides txindex + blockfilterindex; scans run via scanblocks
   └── user DB (SQLite, sensitive, on VeraCrypt volume; file mode 0600), including the chain-data cache
       (nothing the app writes lives on plain disk)
```

### 1. Chain data from Bitcoin Core (`backend/coinacct/chain/`)
The app keeps **no chain index of its own**. Bitcoin Core's built-in indexes answer every chain question on demand, and the app caches the answers it needs in the user DB. Maintaining the node (initial sync, pruning, restores, resyncs) is the user's responsibility; the app only checks its settings.
- **Node requirements, checked at startup** (the app refuses to run with a clear message if any fails):
  - `txindex=1` and `blockfilterindex=1`, both fully synced (`getindexinfo`)
  - **unpruned** node (`getblockchaininfo.pruned == false`)
  - matching chain
  - a minimum Core version: **≥ 25.0** (first release with a stable `scanblocks`), final choice in the M0 ADR
- **RPC access:**
  - The app uses a dedicated `rpcauth` user, restricted in Core by `rpcwhitelist=<user>:<methods>` to read-only methods. The app keeps its own allowlist too, as a second layer.
  - Loopback only.
  - At startup the app calls a harmless method that is *not* on the whitelist (e.g. `uptime`). If that call succeeds, the server-side whitelist is missing, and the app refuses to run.
  - The app never uses the node wallet or the REST interface.
  - Parallel calls are capped below `rpcthreads`/`rpcworkqueue`.
- **How each question is answered:**

  | Question | Core RPC |
  |---|---|
  | Transaction details, and backward expansion (the tx that created an input) | `getrawtransaction <txid> 2`: decoded, with prevout values and scripts (`txindex`) |
  | Address/script history: every tx paying to or spending from a script | `scanblocks start [descriptors] <from> <to>` over the compact block filters → matching block hashes → `getblock <hash> 2`/`3` → keep the txs that touch the script. Scan objects are `addr(...)`, `raw(<script hex>)` for scripts without an address, or ranged descriptors |
  | Descriptor/xpub discovery | One `scanblocks` call with the ranged descriptor (gap-limit range), plus `deriveaddresses` to map hits to indexes |
  | Is this output spent? | `gettxout <txid> <n>` |
  | Forward expansion: which tx spent an output? | If `gettxout` says spent: `scanblocks` for the output's script from the output's height, in chunks, stopping at the first block where it is spent |
  | Chain tip, headers, confirmations | `getblockchaininfo`, `getbestblockhash`, `getblockheader`, `getblockhash` |

- **Scans run as background jobs.** Core runs one `scanblocks` at a time. The app queues scans, shows progress via `scanblocks status`, and can cancel them (`scanblocks abort`). Results arrive incrementally in the UI.
- **Caching:** decoded txs, script histories and spender lookups are cached in the user DB (on the volume, since they reveal which scripts the user cares about). Each cached row records the block hash and height it came from.
- **Reorgs:** on each tip change the app checks the cached rows near the tip against the active chain (`getblockhash`). Rows from blocks no longer on the active chain are invalidated and re-fetched. Nothing depends on an undo log.
- **Perf check (M1):** measure `scanblocks` time for a single script over the whole chain and over typical ranges, and for a ranged descriptor, on a mainnet node, using public scripts that are not the user's (run by the human, per the agent rules in P0.2). If forward expansion is too slow for interactive use, options include scanning smaller ranges first, running scans ahead of time for owned outputs, and batching scripts into one scan. Any change of approach needs an ADR.

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
- `descriptor`: descriptor/xpub, gap limit, next index, tax_account, client(s).
- Chain-data cache (recomputable from the node): `tx_cache` (decoded txs), `utxo_cache` (outputs relevant to the user), `script_history` (script → txs + scanned height range), `spender` (outpoint → spending tx). Every row records its source block hash and height, for reorg checks. `tx.mixing` flag (auto-detected, user-overridable).
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
  - **descriptors/xpubs (v1)**, derived with Core's `deriveaddresses` (no node wallet) under a gap limit that extends as used addresses are found
  - Each import is assigned to an entity (default "me"), a tax account, and wallet client(s).
- **Discovery:** scan each imported script or descriptor with `scanblocks` (batched into as few scans as possible) to get all funding and spending txs. Those are cached, and owned UTXOs (current and historical) are derived.
- **Third-party tags** work the same way (e.g. an employer's address). Suggestion engine:
  - **Common-input heuristic:** addresses co-spent with a tagged address are suggested as the same entity. This is suppressed for txs flagged `mixing` (CoinJoin/PayJoin-like: many equal-value outputs, or set by the user).
  - A tx that pays the user from a tagged cluster produces a suggested event (e.g. `income` from Employer).
  - Suggestions are never auto-applied. The user accepts or rejects each one, rejections are remembered, and the tag records its provenance.

### 4. Graph UI (`frontend/`)
- Cytoscape.js with a DAG layout. Nodes are txs and outputs. Outputs are colored by owning entity, and badges show doxx sets, with certain and inferred links drawn differently.
- Click an output to expand backward (funding tx) or forward (spender, via `gettxout` + a `scanblocks` job; progress is shown and the job can be cancelled). Expansion is lazy and uses paging for large txs.
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
  - Because in-process egress is an accepted risk, **these controls are the primary defence against data exfiltration**.
- **CI:**
  - GitHub Actions pinned by commit SHA; `permissions: contents: read` by default
  - no secrets needed
  - `pip-audit`/`pnpm audit` + Socket on each PR
  - reproducible frontend build
  - `bitcoind` for regtest downloaded with SHA256SUMS + builder-signature verification
  - Playwright browsers pinned
- **Testing:**
  - The strategy leans on E2E: regtest `bitcoind` + backend + Playwright UI flows are the primary evidence a feature works, and unit tests back up the doxx and tax engines and the scan/cache logic.
  - E2E runs on both Linux and macOS.
  - Coverage floors, enforced in CI: overall ≥85% line+branch, and ≥95% for `tax/`, `doxx.py`, `chain/`. These are floors, not targets.
  - A test-time socket guard (patching `socket.connect` **and** `getaddrinfo`) fails any test that opens an unexpected connection. It catches accidental phoning home; it is not a security boundary.
  - **Anti-test-slop:**
    - A review checklist bans tests that only assert mocks, tautologies, snapshot-everything tests, and tests changed to match buggy output.
    - Tax tests use hand-worked expected values derived from IRS rules, independently of the code.
    - Mutation testing (`mutmut`) on `tax/` and `doxx.py` runs on a schedule, with a surviving-mutant budget.
    - A periodic "test audit" task reviews the suite and deletes or strengthens weak tests.
- **AI agents and real data:**
  - Agents never run while a VeraCrypt volume with real data is mounted, and never get access to real DBs, logs or exports.
  - Development and E2E use regtest and synthetic data only.
  - Mainnet smoke tests are run by the human, not by an agent.
  - Data paths are listed in `.gitignore` and agent-ignore files.
- **Design records:**
  - ADRs in `docs/adr/NNNN-title.md` (MADR format) for every significant or irreversible choice. Changing a decision means writing a superseding ADR, not editing history.
  - Architecture diagram changes need an ADR plus human review.
- **Code:**
  - typed Python (mypy strict), TypeScript strict, ruff/eslint
  - integer sats, `Decimal` for fiat; `float` is banned in `tax/`
  - React inline styles are banned, because the CSP has no `'unsafe-inline'`
  - no network calls outside `rpc.py` (loopback) and `prices/`
- **Process:**
  - small commits
  - each PR updates the threat model status and any affected ADR/diagram
  - Definition of Done includes E2E coverage of the feature

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
2. **M1 chain access:** node checks, the tx fetch/decode layer, the `scanblocks` job queue (status/abort), the chain-data cache and reorg invalidation, and the mainnet perf check of `scanblocks`.
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
  - scan-result processing: finding the txs that touch a script in a decoded block, including P2PK/bare multisig via `raw()`, and filter false positives
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
  - force a reorg (`invalidateblock` in the test harness, never in the app)

  Then assert that discovery, forward/backward expansion, cache invalidation, doxx tags and lot flow all match expectations.
- **End-to-end** (Playwright, Linux + macOS): run the backend + UI against regtest and drive the whole flow: import descriptor → expand graph → tag exchange → record sell with timely identification → export 8949. The socket guard log confirms that no unexpected connections were made.
- **Mainnet smoke test:** run **by the human** on their node with a small address set once M1–M3 land. Agents do not take part.
