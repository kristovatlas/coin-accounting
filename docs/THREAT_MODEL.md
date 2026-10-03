# Threat Model — Coin Accounting

> **Living document.** It describes what is *planned* and, over time, what is *implemented* and *verified*.
> Every change that touches a trust boundary, asset, data store, network flow, dependency or tax rule must update this file **in the same PR**. Reviewers reject PRs that don't.

| | |
|---|---|
| Version | 0.7.28 (M0 in progress) |
| Last updated | 2026-10-01 |
| Scope | v1: Bitcoin (Bitcoin Core) only, on Linux and macOS — see [`PLAN.md`](../PLAN.md) |
| Method | Data-flow diagram → trust boundaries → STRIDE per boundary, plus privacy (linkability/disclosure) and integrity-of-tax-output threats |

### Status legend
| Status | Meaning |
|---|---|
| **Planned** | Mitigation is designed but not built |
| **Implemented** | Code exists; link to it |
| **Verified** | An automated test proves the mitigation; link to the test |
| **Documented** | Procedural or documentation-only mitigation is written and linked. This is the highest status such rows can reach |
| **Accepted** | Risk knowingly not mitigated; rationale recorded in §9 |
| **N/A** | No longer applicable (keep the row; say why) |

A threat may only move to **Verified** when a test exists that fails if the mitigation is removed. Rows whose mitigation is (partly) procedural are marked † and can reach at most **Documented** for that part.

---

## 1. System overview

The app is a single-user tool running on the user's own machine (Linux or macOS). Its parts:
- **Launcher + backend:** one process. The launcher applies the process hardening and starts a FastAPI server bound to `127.0.0.1` (architecture §3).
- **UI:** a React + Cytoscape.js single-page app served by the backend and opened in the user's default browser through a one-time bootstrap file (architecture §4).
- **Chain data:** comes only from the user's Bitcoin Core node over **loopback** JSON-RPC, as a dedicated, server-side-whitelisted RPC user. The app keeps **no chain index of its own**: Core's `txindex`, `blockfilterindex` (via `scanblocks`/`getdescriptoractivity`) and `txospenderindex` (via `gettxspendingprevout`) answer chain queries, and the answers are cached in the user DB.
- **Sensitive user data:** stored in a SQLite database kept on a VeraCrypt volume.
- **Outbound internet:** the only allowed flow is a bulk, date-independent download of historical fiat prices, optionally via Tor/SOCKS5.

### 1.1 Data-flow diagram and trust boundaries

```mermaid
flowchart LR
  subgraph TB0["TB0 · User's machine (OS user account)"]
    BOOT[("Bootstrap file 0600<br/>one-time token")]
    subgraph TB1["TB1 · User's default browser"]
      UI["SPA (bundled assets)"]
      OtherTabs["Other websites / tabs /<br/>other localhost apps (untrusted)"]
      EXT["Browser extensions<br/>(untrusted, R-7)"]
    end
    FILES[/"TB6 · Uploads<br/>(address lists, descriptors, price CSV)"/]
    subgraph APP["Launcher + backend process"]
      API["api/ :127.0.0.1"]
      SVC["services/ + pure engines<br/>(tax/, doxx/)"]
      CHAIN["chain/ → rpc.py"]
      PRC["prices/"]
      STORE["storage/"]
    end
    subgraph TB4V["TB4 · VeraCrypt volume (encrypted at rest)"]
      UDB[("User DB (SQLite)<br/>tags, lots, events, prices, chain cache")]
      LOGS[("Logs, exports, config.toml<br/>(incl. RPC credentials)")]
    end
    NODE["Bitcoin Core<br/>(unpruned; txindex + blockfilterindex<br/>+ txospenderindex; rpcwhitelist)"]
  end
  NET(("Internet<br/>price source"))
  SUPPLY(("TB5 · npm / PyPI / GitHub<br/>AI coding agents<br/>(build/dev time)"))

  BOOT -. "file:// open, token claimed once" .-> UI
  UI -- "TB1: HTTP loopback, Authorization: Bearer" --> API
  OtherTabs -. "blocked: Host check, no ambient credential, no CORS" .-> API
  EXT -. "can read the SPA's DOM (accepted, R-7)" .-> UI
  FILES -- "TB6: upload + validation" --> API
  API --> SVC
  SVC --> CHAIN
  SVC --> PRC
  SVC --> STORE
  CHAIN -- "TB2: JSON-RPC, whitelisted methods (rpcauth, loopback)" --> NODE
  CHAIN --> STORE
  PRC -- "TB3: HTTPS, optional local SOCKS5/Tor<br/>bulk, date-independent request" --> NET
  PRC --> STORE
  STORE --> UDB
  STORE --> LOGS
  SUPPLY -. "TB5: dependencies, CI, agents" .-> APP
```

| ID | Boundary | Crosses between |
|---|---|---|
| TB0 | OS account | The user's session ↔ other OS users / remote attackers |
| TB1 | Browser ↔ backend | Our SPA, and any other web origin the browser loads, ↔ the loopback API |
| TB2 | Backend ↔ Bitcoin Core | Our process ↔ node JSON-RPC (loopback only) |
| TB3 | Backend ↔ Internet | Price fetcher ↔ external price/FX source (the **only** runtime outbound flow) |
| TB4 | Process ↔ disk | Encrypted volume (**everything the app writes**) vs plain disk (nothing the app writes; only OS or browser spill, see §5.4) |
| TB5 | Build/dev supply chain | Third-party packages, build tools, CI, AI coding agents ↔ our code and dev environment |
| TB6 | User-supplied files | Imported address lists / descriptors / CSVs ↔ parser |

---

## 2. Assets

| ID | Asset | Sensitivity | Where it lives |
|---|---|---|---|
| A1 | **Ownership map**: owned addresses/descriptors, address↔tax-account and ↔wallet-client mapping, entity tags (self, exchanges, employer, …) | **Critical** (full deanonymization of the user's holdings and counterparties) | User DB (TB4V) |
| A2 | **Doxx map**: which coins are linked to which identity-knowing entity | Critical | User DB |
| A3 | **Tax records**: lots, basis, identifications, disposals, income, exchange accounts | Critical (financial + identity) | User DB, exports |
| A4 | **Chain-data cache**: decoded txs, UTXOs, script histories and spender lookups for the scripts the user cares about | Critical (equivalent to A1) | User DB |
| A5 | **Report exports** (8949 CSV, income, summaries, audit trail) | Critical | Export dir (TB4V) |
| A6 | **Logs** | High if they contain A1–A4 values | TB4V |
| A7 | **Node RPC credentials** (dedicated `rpcauth` user) | Medium: the account is whitelisted to read-only methods, but it still grants query access | App config on TB4V; app memory |
| A8 | ~~Chain index~~ | *Removed in v0.4:* there is no app-side chain index. Cached chain data (decoded txs, script histories, spenders) reveals which scripts the user cares about, so it is part of **A4** | — |
| A9 | **Price history cache** | Public data; integrity-sensitive | User DB |
| A10 | **Correctness of tax output** | High: wrong figures mean legal/financial harm | Engines, rule sets, tests |
| A11 | **Fact of usage**: that this IP / person runs a BTC accounting tool | Medium | Network metadata |
| A12 | **Source code & build integrity** | High (a compromised build exfiltrates A1–A5) | Repo, CI, dependency tree |

## 3. Security & privacy objectives

1. **O1 — No data egress.** A1–A6 never leave the machine through the app. The only outbound connection carries no user-derived data. *(Protection against **malicious code inside the process** relies on supply-chain controls; see R-4.)*
2. **O2 — Local-only chain data.** All chain data comes from the user's own node over loopback. No block explorers, Electrum servers or third-party APIs.
3. **O3 — Encrypted at rest.** All sensitive data (A1–A6) lives only on the VeraCrypt volume. Nothing sensitive spills to plain disk.
4. **O4 — Loopback isolation.** Only the user, through our UI, can reach the API. Other web origins and other OS users cannot.
5. **O5 — Integrity of records and tax output.** Figures follow the supported federal rules for the relevant tax year. They are deterministic, reproducible, auditable, and traceable to their inputs.
6. **O6 — Honest privacy signal.** The doxx view models every link it can determine from the user's data (a lower bound) and never overstates privacy.
7. **O7 — Trustworthy build and dev process.** Dependencies, tooling and coding agents cannot silently add network access or exfiltrate user data.

## 4. Adversaries

| ID | Adversary | Capabilities | In scope? |
|---|---|---|---|
| AD1 | **Malicious website** visited in the same browser | JS in another origin; DNS rebinding; CSRF; timing | Yes |
| AD2 | **Other local OS user** / other process under another UID | Can connect to loopback ports; read world-readable files | Yes |
| AD3 | **Device thief / cold-disk attacker** (laptop stolen, disk imaged while the volume is dismounted) | Offline access to plain disk, swap, browser data outside the volume | Yes |
| AD4 | **Network observer** (ISP, Wi-Fi, state, price-source operator) | Sees outbound connections, DNS, TLS metadata, request timing | Yes |
| AD5 | **Compromised/malicious price source** or MITM | Serves wrong prices | Yes |
| AD6 | **Supply-chain attacker** (typosquat, hijacked maintainer, malicious GitHub Action, tampered release/`bitcoind` download) | Code execution at install/build/runtime | Yes |
| AD7 | **Adversarial on-chain data** (crafted tx/script/OP_RETURN content by any payer) | Controls bytes that Core decodes and our result processing and UI handle | Yes |
| AD8 | **Chain-analysis firm / exchange / identity-knowing counterparty** correlating the user's coins | Public blockchain + KYC or other identity data | Yes (as a *privacy* adversary the doxx model informs against) |
| AD9 | **The user themself** (mistakes, mis-tagging, wrong paths, accidental export, late identification) | Legitimate access | Yes (safety rails) |
| AD12 | **Cloud-backed AI coding agent** used during development | Reads files and terminal output on the dev machine and sends them to a remote model provider | Yes (as a dev-process exfiltration path) |
| AD10 | **Malware running as the same OS user**, root/kernel compromise, hardware implants | Full access while the volume is mounted | **No** (see §8) |
| AD11 | **Physical coercion** | — | No |

---

## 5. Threat register

Columns: **ID** · **STRIDE/P** (S spoofing, T tampering, R repudiation, I info disclosure, D denial of service, E elevation, P privacy/linkability) · **Threat** · **Mitigation (planned)** · **Status** · **Evidence** (code / test / doc link, filled when implemented). † = partly procedural.

### 5.1 TB1 — Browser ↔ backend

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-101 | I, T | **DNS rebinding**: an AD1 page rebinds its hostname to 127.0.0.1 and reads or modifies the API as a same-origin request | Strict `Host` header allowlist: exactly `127.0.0.1:<port>` (one canonical hostname, never `localhost`, so the SPA origin, and with it the `sessionStorage` session, is always the same). Every other Host is rejected before routing | Planned | |
| T-102 | T | **CSRF**: an AD1 page, or another app on `127.0.0.1`, sends state-changing requests to the API | **No ambient credentials:** no cookies. Every request needs `Authorization: Bearer <session>`, and the session token is held in `sessionStorage`, which is scoped to the full origin including the port, so other pages can't read it or attach it. Only JSON bodies are accepted; **no CORS headers ever** (architecture §4) | Planned | |
| T-103 | I, S | **Other local users (AD2)**, or other web apps on `127.0.0.1`, reach the API or steal the session | Bind `127.0.0.1` only (never `0.0.0.0`); random port. **Bootstrap:** the launcher writes a one-time token (60 s TTL, single use) into a mode-0600 file (Linux: `$XDG_RUNTIME_DIR`; macOS: the data directory), and opens that *file* in the browser. So the token never appears in any process's argv (`/proc/<pid>/cmdline`, `ps` are readable by other users). The SPA exchanges it once for a session token that lives until the backend exits. Cookies are not used, because they are shared by every port on the host | Planned | |
| T-104 | T, E | **XSS via attacker-controlled chain data (AD7)**, e.g. OP_RETURN text or crafted labels rendered in the graph or tables | React escaping only (lint bans `dangerouslySetInnerHTML` and inline styles); Cytoscape labels as text. Strict CSP: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` | Planned | |
| T-105 | I | **Browser leaks sensitive data to plain disk (AD3)**: history, HTTP cache, autofill, session restore, crash reports, thumbnails | App-side, browser-independent controls: no sensitive values in URLs, including the launch token (T-110); opaque IDs; queries in POST bodies; `Cache-Control: no-store` on all API responses; autocomplete off on sensitive fields. v1 opens the user's **default browser** and does **not** manage a dedicated profile. Whatever the browser still writes is accepted as **R-5** (user decision, 2026-09-27) | Planned (app-side) / Accepted (browser-side) | |
| T-106 | I, P | **UI pulls third-party resources** (fonts, CDN, analytics), leaking usage (A11) or data | All assets bundled at build time; CSP `connect-src 'self'`; a CI test fails if the built bundle references external URLs | Planned | |
| T-107 | T | **Clickjacking**: an AD1 page frames the UI | `frame-ancestors 'none'` + `X-Frame-Options: DENY` | Planned | |
| T-108 | I | **Browser downloads of exports go to `~/Downloads`** (plain disk) | Exports are written server-side only to `<data>/exports/` on the volume. A browser download is the only other destination: it needs an explicit confirmation that warns about the destination, and uses a POST with the bearer header that returns a blob (never a GET link that another localhost app could trigger). The browser, not the app, writes the downloaded file (R-5) | Planned | |
| T-109 | I | **Clipboard and screen exposure**: copied addresses or txids reach clipboard managers or sync (plain disk, cloud); screenshots or screen sharing | Copy actions show a notice; clipboard sync and screen recording are covered in the user docs | Documented † | |
| T-110 | I | **Launch credential leaks** via process arguments, browser history, session restore or referrer | The token comes from the bootstrap file, not argv. It travels in the URL **fragment** (never sent to servers or in the Referer), is single-use with a 60 s TTL, and is removed with `history.replaceState` before rendering. `Referrer-Policy: no-referrer`. A second claim is refused, logged, and shows "Session already claimed. Restart the app." The bootstrap file is deleted on claim or expiry | Implemented (partly) | Launcher side: `launcher.write_bootstrap_file` writes a 0600 HTML file (`O_EXCL`, `O_NOFOLLOW`) in `$XDG_RUNTIME_DIR` when it is the user's own 0700 directory, else in `<data>`; the token (32 random bytes) appears only in the URL fragment; `no-referrer`; no script. Tests `unit/launcher/test_launcher.py` (`*_t110`). Pending: claim, single use, 60 s expiry, deletion and `history.replaceState` (the web app) |
| T-111 | I | **Browser extensions** in the user's default profile, with access to all sites, can read the SPA's pages (ownership map, doxx map, tax data) and send them anywhere | Not preventable by the app. The docs recommend a **private window or a clean profile with extensions disabled**; Chromium incognito and Firefox private windows disable most extensions by default. Accepted as **R-7** | Accepted † | |

### 5.2 TB2 — Backend ↔ Bitcoin Core

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-201 | I | **RPC credential leakage** (A7) through logs, the DB, error pages or exports | A dedicated `rpcauth` user whose password is stored only in the app config on the volume; never logged or echoed; the redaction filter covers auth headers. The app does **not** read Core's cookie file, which is unrestricted | Implemented (partly) | `coinacct/domain/secret.py` (`Secret` never formats its value), `config.py` and `rpc.py`: errors never carry the password or call parameters, and a node error's text (which Core uses to quote the offending parameter, checked on regtest 31.1) stays out of `str`/`repr`, in `RpcCallError.node_message` only. Tests `test_secret.py`, `unit/config/test_config.py`, `unit/rpc/test_rpc.py` (`*_t201_t403`, `test_a_node_errors_text_is_kept_apart_from_its_string_form_t403`). Pending: the redacting log handler (T-403) |
| T-202 | I, P | **Queries sent to a non-local node**: RPC has no TLS, so credentials and every lookup (A1) would cross the network in plaintext | **Loopback only; non-loopback RPC endpoints are refused, with no override.** For a node elsewhere, the docs cover an SSH tunnel (`ssh -L`) that terminates on loopback | Implemented (partly) | Refused twice, with no override: `config.py` accepts only a literal loopback IP (names, even `localhost`, are refused) and `RpcClient` refuses any other endpoint. Tests `test_non_loopback_*_t202`, `test_host_names_are_refused_even_localhost_t202`; removing either check fails them. Pending: the launcher wiring (M0.3) |
| T-203 | E, T | **App (or code inside it) misuses the node**: wallet, broadcast or other state-changing RPCs (e.g. `sendrawtransaction`, or `importdescriptors` storing the user's addresses in a node wallet **outside** the volume) | **Server-side `rpcwhitelist`** for the app's rpcauth user, limited to read-only methods: `getblockchaininfo`, `getnetworkinfo`, `getindexinfo`, `getblockcount`, `getbestblockhash`, `getblockhash`, `getblockheader`, `getblock`, `getrawtransaction`, `gettxout`, `gettxspendingprevout`, `scanblocks`, `getdescriptoractivity`, `getchaintips`, `deriveaddresses`, `getdescriptorinfo`. A client-side allowlist adds defence in depth. **Canary at startup:** the app calls a harmless method that is *not* whitelisted (`uptime`) and refuses to run if it succeeds. REST (`rest=1`) is never used, and the docs recommend leaving it off | Implemented (partly) | Client allowlist and canary in `coinacct/rpc.py` (`ALLOWED_METHODS`, `canary_refused`: only HTTP 403 counts as refused), checked at startup by `coinacct/chain/node_checks.py`. Unit tests `unit/rpc/test_rpc.py` (`*_t203`); on regtest 31.1 (`integration/chain/test_node_checks_regtest.py`): the whitelisted node refuses the canary and a wallet method even with the client allowlist bypassed, a node without `rpcwhitelist` fails the canary, and a whitelist missing a needed method is reported. Pending: offline mode |
| T-204 | P | **Node logs reveal lookup activity** | With `debug=rpc`, Core logs method names and users but not parameters. The docs recommend not running with `debug=rpc`/`debug=http` | Documented † | |
| T-205 | D, T | **Adversarial or oversized chain data (AD7)**: huge blocks/txs or **very busy scripts** (e.g. a tagged exchange hot wallet with 100k+ blocks of activity) exhaust memory, time or disk; unusual scripts break processing; crafted text reaches the UI (T-104) | No custom binary parser: Core decodes. Response size limits and streaming JSON decoding; `getdescriptoractivity` calls capped at a few hundred blocks; a per-script activity budget (candidate count from `scanblocks` first; above a threshold, warn and ask); clustering never auto-expands busy scripts; property/fuzz tests on result processing; fails closed (no partial cache writes) | Planned | |
| T-206 | T | **Wrong network**: a testnet/signet/regtest node is used against a mainnet DB, or the reverse | Store `chain` in the user DB; refuse to start on mismatch | Implemented (partly) | `node_checks.evaluate` reports a chain that differs from the data directory's (`test_a_chain_other_than_the_data_directorys_is_refused_t206`). Pending: storing `chain` in the user DB (M2) |
| T-207 | T | **Reorgs or new blocks leave cached chain data stale or incomplete**: rows from orphaned blocks, **"nothing found" results and scanned ranges** that no longer reflect the active chain, reorgs that happened while the app was closed, stale mempool results | Every block-backed row stores its block hash and height; each script's coverage stores its range and stop-block hash; negative answers are **snapshots** tagged with the tip hash. A persisted last-seen tip: on startup and every tip change, find the fork point (walk `getblockheader` while `confirmations == -1`), invalidate every row, coverage range and snapshot above it **at any depth**, then extend coverage to the new tip. Mempool results are kept separately and rebuilt on every refresh. Tax events become final only after a configurable number of confirmations (default 6) | Planned | |
| T-208 | T | ~~Index prefix collisions~~ → **BIP30 duplicate txids** | Prefix collisions are *N/A since v0.4* (no app-side index). **BIP30:** Core's `txindex` keeps only the later of each duplicate coinbase txid, so the app keys cached txs by (txid, block hash) and always fetches with the block hash from the activity event. Duplicate outpoints are resolved chronologically. The earlier outputs are unspendable | Planned | |
| T-209 | I, P | **Queries leave traces outside the volume**: the node or the app records which scripts or txs the user looked up, or that the tool is in use | App side: all cached chain data lives in the user DB on the volume. Node side: no node wallet is used, and Core doesn't persist RPC parameters or scan descriptors. `debug=rpc` would log method, user and request `id`, so the app uses counter `id`s. **Known trace:** the startup canary's refused call writes an unconditional warning to `debug.log` naming the RPC user. The docs recommend a generic username (fact-of-usage leak, A11). **M0 check:** after a regtest run, search the node's datadir for the scanned descriptors and txids; the only expected hit is that canary line | Implemented (partly) | Counter request ids in `coinacct/rpc.py` (`test_request_ids_are_a_plain_counter_t209`). **M0 check done** on regtest 31.1 (`test_lookups_and_scans_leave_no_trace_in_the_node_datadir_except_the_canary_line_t209`), with default logging and again with `-debug=rpc -debug=http`. A descriptor built from a BIP32 test-vector key is scanned with `scanblocks` and `getdescriptoractivity`, finding a block mined to one of its addresses, and a txid is looked up with `getrawtransaction`. Afterwards no file in the node's datadir contains the key, the derived addresses, the scanned txid or the looked-up txid as text. `debug.log` holds the expected `RPC User ro-client not allowed to call method uptime` line. Not covered: binary encodings (e.g. raw script bytes in a file) |
| T-210 | D, T | **Incomplete chain data**: a pruned node, a missing or lagging index, or **`scanblocks` silently skipping filter ranges it can't read**. Core returns `completed: true` and `to_height == stop` anyway, so neither signals the gap | Startup: unpruned; `txindex`, `blockfilterindex`, `txospenderindex` each `synced` with `best_block_height == getblockcount()`. **Scan protocol** (PLAN §1): stop height ≤ the filter index height and ≤ tip − 100; bounded ranges; after each range, the index height and `getblockhash(stop)` are re-checked, else discard and retry; the newest ~100 blocks are only ever read via `getdescriptoractivity`, which errors rather than skips; "Block is not in main chain" triggers a rescan; any other failure is a hard error. Forward expansion uses `txospenderindex` (no filters, waits for index sync, errors loudly). **Residual:** read errors or corruption inside the node's filter index are still skipped silently. Covered by trusting the node's indexes (§8); reported upstream | Implemented (partly) | Startup part: `node_checks` requires an unpruned node and the three indexes each `synced` with `best_block_height == getblockcount()`, retried briefly (unit tests `unit/chain/test_node_checks.py`, `*_t210`; on regtest 31.1, a node missing each index is refused). Pending: the scan protocol (M1) |
| T-211 | T | ~~Chain index tampered with on plain disk~~ | *N/A since v0.4:* the app writes nothing to plain disk. The node's indexes are trusted (§8). The chain cache on the volume is recomputable and is kept consistent by T-207/T-210 | N/A | |
| T-212 | D | **Long-running or orphaned scans**: Core runs one `scanblocks` at a time **for all RPC users**, a scan keeps running after a client disconnect or crash, and `status`/`abort` can't tell whose scan it is | Scans are queued background jobs over bounded ranges, with `status`/`abort` on a second connection. "Scan already in progress" means *queue busy* and is retried with backoff. At startup and after any client error, the app checks `status` and aborts a scan it left running. Client timeouts are long but finite. Forward expansion doesn't use scans at all (`txospenderindex`) | Planned | |

### 5.3 TB3 — Backend ↔ Internet (price data)

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-301 | P | **Request content reveals acquisition/sale dates, holdings or residency** | Only bulk, **date-independent** requests. The request set never depends on user records (events, addresses, tags); only the start of the incremental range depends on the public price cache. **All supported fiat pairs are always fetched**, so the chosen currency isn't revealed. A test asserts the requests are identical for different user DBs with the same price cache | Planned | |
| T-302 | P | **Network observer (AD4) learns that this IP runs a BTC accounting tool** (A11) | Optional SOCKS5 proxy (Tor) with remote DNS (`socks5h`); a common browser User-Agent (not a library or unique string); fetches only on manual trigger; the first run prompts the user to choose proxy or direct | Planned † | |
| T-303 | T | **Tampered or incorrect prices (AD5)**: MITM or compromised source → wrong basis/proceeds (A10) | TLS with certificate verification (no opt-out flag); sanity checks (gaps, non-positive values, day-over-day outlier threshold); store source, method and content hash per import; every price can be overridden by the user, and overrides are flagged in reports | Planned | |
| T-304 | D | **Price source disappears or changes format** | The local cache is authoritative once fetched; a CSV import fallback; parser errors fail loudly without partial overwrite | Planned | |
| T-305 | I | **Accidental unexpected outbound connection** (a dependency with telemetry or update checks, a coding mistake) | Only `rpc.py` (loopback) and `prices/` open connections, enforced by code review and structure. A test-time socket guard patches `socket.connect` **and** `getaddrinfo` and fails any test that opens a non-allowlisted connection or DNS lookup. **Not a security boundary** against malicious code in the process (R-4) | Implemented (partly) | Socket guard `backend/tests/socket_guard.py`, installed by `pytest_configure` (tryfirst) in `backend/tests/conftest.py` before test modules are collected, and removed by `pytest_unconfigure` (trylast). `connect`, `connect_ex`, `sendto`, `sendmsg`, `getaddrinfo` and the legacy resolver calls fail for anything but loopback. Every block is also **recorded**, so a test still fails when the code under test swallows the error (`except Exception`), and blocks outside any test fail the session. Tests `backend/tests/unit/test_socket_guard.py` and `test_socket_guard_at_import.py`. Not covered: code that runs before `pytest_configure` (interpreter, coverage and `-p` plugin start-up). Runtime structure (only `rpc.py`, `prices/` and the launcher's server bind use the network) is checked by `scripts/check_architecture.py`. Pending: CI doesn't run `make test` yet (#44) |

### 5.4 TB4 — Data at rest

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-401 | I | **User DB created on plain disk by mistake (AD9 → AD3)** | At startup, resolve the real path of the DB (following symlinks) and identify the block device it lives on. **Linux:** `stat().st_dev` → `/sys/dev/block/<maj>:<min>/dm/{name,uuid}`; accept `veracrypt*` names and `CRYPT-TCRYPT*` / `CRYPT-LUKS*` uuids. **macOS:** detection method to be designed in M0 (VeraCrypt mounts through macFUSE/FUSE-T); if it can't be done reliably, the user confirms the path explicitly and the confirmation is stored per path. Otherwise refuse to start. `--allow-unencrypted-storage` is accepted **only for regtest, signet and testnet** (development and CI), never on mainnet. The data directory is passed with `--data-dir` or `COINACCT_DATA_DIR` each time and is never saved on plain disk. Warn on FAT/exFAT (no permission bits). DB and side files use mode 0600 with a restrictive umask | Implemented (partly) | `coinacct/storage/volume.py`: Linux detection exactly as listed here (sysfs `dm/name` `veracrypt*`, `dm/uuid` `CRYPT-TCRYPT*`/`CRYPT-LUKS*`; anything else, including tmpfs and devices without device-mapper, is unencrypted), and the filesystem type from `/proc/self/mountinfo` for the FAT/exFAT warning. **macOS uses the documented fallback** (owner decision, 2026-10-02: no detection method for now): an explicit confirmation (`--confirm-encrypted-volume`) stored in `<data>`, tied to the directory's real path, device and inode, mode 0600, never followed through a link. `coinacct/storage/datadir.py`: no default data dir; everything under `<data>` must be on the classified device (subdirectories, `DataDir.path()` through the nearest existing part, `config.toml` through `fstat`, and `clear_tmp`, which never crosses into another filesystem); the directory must be owned by the user with no group/other access; `storage_refusal` allows `--allow-unencrypted-storage` only on regtest/signet/testnet (an unknown chain counts as mainnet). `launcher.prepare` refuses an unencrypted directory without the flag, and with it marks start-up as needing a test chain (enforced once the node's chain is known, with the web app). Tests `backend/tests/unit/storage/`, `unit/launcher/` (`*_t401`), mutation-checked |
| T-402 | I | **SQLite side files and temp data spill** (WAL, journal, temp B-trees, sort files, HTTP download buffers) | Side files live next to the DB on the volume; `PRAGMA temp_store=MEMORY`; `TMPDIR`/`SQLITE_TMPDIR` for the process point to a directory on the volume | Implemented (partly) | `launcher.use_temp_dir` points `TMPDIR`, `SQLITE_TMPDIR` and `tempfile.tempdir` at `<data>/tmp` (0700), which is cleared at start without following links (`test_temp_files_go_to_the_volume_and_tmp_is_cleared_t402`, `test_clearing_tmp_removes_links_without_following_them`). Pending: `temp_store=MEMORY` with the DB |
| T-403 | I | **Logs contain addresses/txids/amounts** (A6) | Logs are written only to the volume; a redaction filter masks addresses, txids, outpoints, descriptors, amounts and fiat values by default; a debug mode that disables redaction is explicit and writes only to the volume; uvicorn, framework and library loggers, `sys.excepthook` and `threading.excepthook` all go through the redacting handler, and the access log is off; tests assert that redaction works | Implemented (partly) | `coinacct/domain/redact.py` masks keys and descriptors, WIF keys, 64+ hex digits (txids, hashes, pubkeys), bech32 and base58 addresses, fiat with a currency sign, decimal amounts and long integers. `storage/logfile.py` writes `<data>/logs/coinacct.log` (0600, never through a link) and redacts after formatting, tracebacks included. `launcher.install_logging` routes the root logger, warnings, `sys.excepthook` and `threading.excepthook` through it. Tests `test_redact.py`, `storage/test_logfile.py`, `launcher/test_launcher.py` (`*_t403`). Pending: uvicorn's loggers, access log off, explicit debug mode |
| T-404 | I | **Swap, hibernation or core dumps** write memory with A1–A4 to plain disk | `RLIMIT_CORE=0`, plus `prctl(PR_SET_DUMPABLE, 0)` on Linux. Docs require encrypted swap and hibernation, or none (macOS encrypts swap by default) | Implemented † | `launcher.harden`: `RLIMIT_CORE=0`, `prctl(PR_SET_DUMPABLE, 0)` on Linux, umask 077; fails closed. Test `test_hardening_disables_core_dumps_and_dumpability_and_sets_a_private_umask_t404` runs it in a child process and reads the limits back. The swap and hibernation part stays documentation (†) |
| T-405 | I | **Data remains accessible after the volume is dismounted** | The watchdog detects that the volume or DB path has disappeared and makes the backend exit immediately; the UI shows that it is disconnected. **Partial:** the OS page cache and freed memory may still hold data until overwritten. VeraCrypt refuses a normal dismount while files are open, so the documented v1 workflow is: quit the app (its browser tab shows it is disconnected), then dismount. An in-app lock is deferred (§10) | Implemented (partly) | `coinacct/storage/watchdog.py`: every 2 s, the verified data directory must still be the same path, device and inode; otherwise it calls the shutdown callback once and stops (tests `unit/storage/test_watchdog.py`, `*_t405`; removing the identity check or the callback fails them). Pending: wiring it to the app's shutdown and the UI's disconnected state |
| T-406 | I | **User data accidentally committed to git or synced**, e.g. a data dir inside the repo checkout, or a volume container placed in cloud sync | There is no default data dir: it is passed with `--data-dir`/`COINACCT_DATA_DIR` each time (architecture §6). `storage/` refuses a data dir inside a git working tree. `.gitignore` and agent-ignore files cover `*.sqlite*`, `data/`, `exports/`. The docs cover backups and cloud sync of the container | Implemented (partly) | `open_data_dir` refuses a data directory inside a git working tree, recognised as git does (a `.git` directory with `HEAD`, or a `gitdir:` file); test `test_a_directory_inside_a_git_working_tree_is_refused_t406`. The ignore files and docs parts are unchanged |
| T-407 | I | **Plaintext exports on plain disk** (see T-108) | The export dir defaults to the volume; the warning covers every alternative path | Planned | |
| T-408 | T, R | **Silent tampering or accidental edits of records** | Append-only change log for events, tags, identifications and overrides (what, when, old → new); `PRAGMA integrity_check` on startup; a documented backup procedure (copy while the volume is mounted) | Planned | |

### 5.5 Tax correctness & privacy signal (A10, O5, O6)

Tax-rule correctness is **in scope**. The supported federal rules are versioned by tax year and reference IRS forms, instructions and guidance. What is out of scope is personalized advice and scenarios explicitly listed as unsupported (§8).

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-501 | T | **Wrong tax figures from bugs** in lot flow, fees, holding period, identification or box selection | Pure, deterministic engine recomputed from events; rule sets per tax year with references; hand-worked expected values derived from IRS rules; mutation testing on `tax/`; coverage floor ≥95% on `tax/` | Planned | |
| T-502 | T | **Numeric errors** (float rounding, overflow) | Integer satoshis; `Decimal` for USD with explicit rounding rules; a lint rule bans `float` in `tax/` | Planned | |
| T-503 | R | **Reports can't be reproduced or audited later** | Each report embeds app version, rule-set version, settings, and a hash of the input events and price rows; the audit trail export links every figure to its events, identifications and txids | Planned | |
| T-504 | T | **Heuristic misattribution**: common-input clustering links wrong addresses (CoinJoin, PayJoin, batched exchange withdrawals) and produces wrong income or entity tags | Heuristics only *suggest*, never auto-apply; txs flagged `mixing` (auto-detected: many equal-value outputs; or set by the user) suppress co-spend clustering; every tag records its provenance | Planned | |
| T-505 | P | **False sense of privacy from unmodeled heuristics**: chain-analysis firms (AD8) link coins through amount/timing correlation, change detection, peel chains, dust, or off-chain data | UI wording is "known links", never "private" or "clean"; the docs list the modeled vs unmodeled heuristics; a persistent note appears in the sell-planner view | Planned † | |
| T-506 | T | **Unconfirmed or reorged txs** flow into tax records | Confirmation threshold (T-207); reorg-driven changes invalidate derived events and surface them for review; unconfirmed items block reports | Planned | |
| T-507 | T | **Unsupported situations silently mis-reported**: lost/stolen, forks/airdrops, state tax, anything outside the supported rules | The unsupported list is shown in the docs and on every report; the UI warns when data suggests one applies; settings that affect tax treatment are printed on every report | Planned † | |
| T-508 | T | **Late or unclear lot identification**, for exchange sales, exchange withdrawals and self-custody spends. A choice made after the sale or transfer may not count: the IRS may apply the account's standing order, or FIFO if there is none, and the figures then differ from the user's (Treas. Reg. §1.1012-1(j)) | Each exchange lot choice stores `identified_at`. Accounts can use their standing method automatically (ADR 0021), which is never late. A manual choice made late is flagged **only if its lots differ** from the standing method's (or FIFO's). **Warn only** (user decision, 2026-09-27): the warning shows the result under the standing order or FIFO, and the flag appears in the audit trail and on the report, but the user's choice is kept. Self-custody: the spent UTXO is the identification (a stated tax position, ADR 0008), and inside a UTXO that holds several lots, fragments leave oldest first unless the wallet has a recorded method (user decision, 2026-09-28). From 2027, a reminder says exchange choices must be given to the broker (Notice 2025-7 relief through 12/31/2026 per Notice 2026-20) | Planned | |
| T-509 | T | **Unknown or incorrect basis**: withdrawals with no recorded buys, the 1/1/2025 per-account transition (Rev. Proc. 2024-28) not reflected, a gift from a donor with unknown basis | `opening_allocation_2025` events per tax account; withdrawals move lots and never create them silently; unknown basis **blocks report generation** until the user resolves it explicitly (e.g. zero basis), and the resolution is recorded | Planned | |
| T-510 | T | **Wrong Form 8949 box**: e.g. digital assets reported in C/F for 2025+, box set per account instead of per disposal | Box chosen per disposal from (tax year, channel, 1099-DA received, basis reported); 2025+ uses G/H/I and J/K/L; on-chain disposals → I/L; the user can override, and the reason is recorded; tests per tax year | Planned | |
| T-511 | P | **Doxx under-claim from deterministic links not modeled**: address reuse, receipts from identity-knowing parties, and payees other than exchanges | Doxx applies to every entity with `knows_identity`. Modeled rules: paid-to, received-from (from confirmed events), address reuse, forward, and backward cluster (*inferred*). CoinJoin `mixing` txs downgrade forward propagation to *inferred*. Rules are recorded in an ADR and tested on hand-built graphs | Planned | |

### 5.6 TB5 — Supply chain & development process

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-601 | E, I | **Malicious or compromised package** (npm/PyPI typosquat, hijacked maintainer, freshly published malicious version, lockfile poisoning). Because in-process egress is accepted (R-4), **this is the primary exfiltration path** | Packages are **resolved, vetted and approved before anything is installed** (ENGINEERING §2.4); `make bootstrap` installs only dependency files and install-affecting config (`.pnpmfile.*`, `.npmrc`, `uv.toml`, `pnpm-workspace.yaml`) merged to `main`, and `toolchain.py install` only pins merged to `main` (checked against `refs/remotes/origin/main`, ignored files included), unless the human passes `DEPS_APPROVED=1` on the make command line (an inherited environment variable doesn't count, but an inherited `MAKEFLAGS` would). Agents never set it: AGENTS.md says so for every agent, and the refusal messages tell agents to stop and ask; only Claude Code also has a technical block (the guard hook). The gate checks the current branch, so it guards against accidental unmerged changes, not a malicious branch (ENGINEERING §2.4). CI is the one other place that passes it (T-608, §5.6.1). 7-day cooldowns (pnpm `minimumReleaseAge` strict, uv relative `exclude-newer`). Every install goes through Socket Firewall via `make` targets, with no fallback. A **lockfile policy check** enforces registry-only sources, hashes and package age on every entry, including transitive ones. Exotic sources are blocked. Monthly batched updates; weekly clean-cache re-scan; `make audit` (`osv-scanner`, ADR 0027; a committed `osv-scanner.toml` is rejected and never loaded) on every PR, once its CI step lands. Details in `docs/ENGINEERING.md` §2 | Planned | |
| T-602 | E | **Install-time or load-time code execution**: dependency build scripts, our own lifecycle scripts, `.pnpmfile` hooks, sdist builds, `.pth` files in wheels, package managers auto-installing or auto-downloading | pnpm `allowBuilds: {}` + `strictDepBuilds`; no lifecycle scripts, `.pnpmfile` or `configDependencies` in our repo (CI check); uv `no-build` with no exceptions and `package = false`, and `make propose-py` passes `--no-build` on the command line; pnpm `ignorePnpmfile: true`, and `make propose-*` refuse unapproved install config; `.pth` allowlist check (uv's `_virtualenv.pth` and, per ADR 0025, coverage's `a1_coverage.pth`, each pinned by content); `verifyDepsBeforeRun: error`, `pmOnFail: error`, `UV_NO_SYNC`, `python-downloads = "never"` | Planned | |
| T-603 | E, T | **Compromised CI, GitHub Actions or toolchain/test downloads** | Actions pinned by full SHA and checked with `zizmor`/`actionlint`; `contents: read`, read-only default token, `persist-credentials: false`, no secrets, no dependency caches; fork PRs need approval. Toolchain and test binaries (`sfw`, pnpm, uv, Node, Python, `bitcoind`, Playwright browsers, actionlint, zizmor, osv-scanner) are each verified per ENGINEERING §2.3; zizmor's online audits catch impostor-commit pins (ADR 0026). **`sfw` itself has no published checksums**, so its committed hash is trust-on-first-use. Limit: the installed-tree digest is recorded in `.toolchain/…/.installed.json` at install time, so it detects accidental or partial changes, not a local attacker who rewrites the files and the marker together (that attacker can equally edit the scripts; R-6) | Implemented (partly) | Implemented: toolchain hash verification, and re-verification of every installed file (Python bytecode included) of sfw, pnpm, uv, Node and Python before each install (`scripts/toolchain.py`, `scripts/toolchain.lock`; tests in `scripts/tests/test_check_adrs_and_toolchain.py`) and the SHA-pinned `actions/checkout` with `contents: read` and `persist-credentials: false` (`.github/workflows/ci.yml`). Implemented by PR #82: pinned actionlint and zizmor (`make lint-tools`, verified before each `make lint-workflows`), and `scripts/check_repo_files.py` rejecting linter config and zizmor ignore comments. Pending: running them in CI (#94), Node `.asc` and `bitcoind` builder-signature checks, Playwright browser hashes (#15) |
| T-604 | I | **Runtime dependency adds network access** (telemetry, update checks) | The socket guard in tests (T-305); dependency review checks for network capability | Planned | |
| T-605 | T | **AI coding agent introduces insecure code, weak tests or unvetted dependencies, or merges its own PRs** | `AGENTS.md` binds agents to this document; the install-command guard catches **accidental or habitual** unwrapped installs only (ADR 0022; deliberate evasion is R-8) and `ENGINEERING.md`; human review of every PR, focused by the review panel's **tripwire** flags (a mechanical scan plus an Opus check for malicious patterns, on the exact commit handed over; ADR 0020); test-slop audits; agents may propose but never approve dependencies. **Only the human merges**: this is procedural, since agents use the owner's GitHub credentials (user decision, 2026-09-27) | Documented † (hook implemented) | [`AGENTS.md`](../AGENTS.md), [ADR 0018](adr/0018-repository-governance.md), [ADR 0020](adr/0020-review-panel.md); tripwire `scripts/tripwire.py` (tests in `scripts/tests/test_tripwire.py`); banned-command hook `scripts/agent_guard.py` via `.claude/settings.json` (Claude Code only), tests in `scripts/tests/test_guards.py` |
| T-606 | T | **Tampered release/source** as distributed to the user | Commit signing is **not** required (user decision, 2026-09-27: agents would need the owner's key). GitHub signs merge commits. If releases are published for other users, release tags will be signed, and a reproducible build with checksums will be considered | Accepted (for now) | |
| T-607 | I | **Cloud-backed AI coding agents (AD12) read real user data** (DB, logs, exports, terminal output) and send it to a model provider | Agents never run while a volume with real data is mounted, and never get access to real data paths; development and E2E use regtest and synthetic data only; mainnet smoke tests are run by the human; agent-ignore files list the data paths; log redaction is on by default in dev. **Claude Code hook** (`scripts/agent_guard.py`): blocks every tool call while a VeraCrypt volume appears to be mounted. Limits: detection matches `veracrypt` in the device-mapper names and mount table, so a volume opened under another mapper name is missed, and an agent could edit the hook itself while no volume is mounted (#13). Other agents rely on the AGENTS.md check | Implemented † (Claude Code hook) | `scripts/agent_guard.py` via `.claude/settings.json`; test `test_blocks_everything_when_veracrypt_is_mounted` (detection itself untested: #22) |
| T-608 | E, I | **A new dependency runs before it is approved**, because it must be installed to be tested, and it must be tested to be approved (the approval chicken-and-egg, §5.6.1) | Unapproved dependencies are installed **only in CI**, never on a developer machine: `ci.yml` passes `DEPS_APPROVED=1` on the make command line (not keyed on `CI`/`GITHUB_ACTIONS`, which anyone can set). CI runners are GitHub-hosted and fresh per job, the installing jobs use only the `pull_request` trigger, and runners hold no secrets and have a read-only token (T-603); Socket reports on the PR; the human approves before merge; locally, only merged dependencies install (T-601) | Planned (M0.2, #44) | Decision: user, 2026-09-28 ([#44](https://github.com/kristovatlas/coin-accounting/issues/44)) |

### 5.6.1 The approval chicken-and-egg (a lesson for other projects too)

This applies to almost any project that vets its dependencies.

**The problem.** A new dependency should not run before someone has approved it. But the best evidence for approving it (do the tests pass? does the audit find anything?) comes from installing and running it. Installing a package can itself run the package's code: install scripts, build steps, and every import when the tests start. So "install it to check it" already means "run code nobody has approved yet". Where that happens decides how much harm a malicious package can do.

**The usual mistakes:**
- **Test it on your own machine.** The package runs next to your SSH keys, browser sessions, tokens and, here, possibly real financial data (R-6). This is exactly the attack a malicious package is built for.
- **Refuse to install anything unapproved, anywhere.** Then nothing can be tested before approval. People approve blind, or work around the rule under deadline pressure, and a control that is routinely bypassed protects nothing.
- **Relax the rule "when running in CI" by checking an environment variable** such as `CI=true`. Anything can set that variable, including an agent or a script on a developer machine, so the exception leaks everywhere.

**Our answer: split where unapproved code may run.**
- **Developer machines, where the valuable things are:** only dependencies already merged to `main` are installed. The human can override this for one command (`DEPS_APPROVED=1` on the make command line) after approving. Agents must not: in Claude Code a hook blocks it, and for other agents it is a written rule in AGENTS.md (T-601).
- **CI, a throwaway machine with nothing worth stealing:** unapproved dependencies are installed and tested there. CI passes `DEPS_APPROVED=1` explicitly in its workflow file, where every change to it is visible in review. It never infers approval from an environment variable (T-608).
- **CI is only safe for this while it holds nothing of value:**
  - no secrets
  - a read-only token (`contents: read`, `persist-credentials: false`)
  - no shared dependency cache that a later, trusted run would reuse
  - no publishing or deploying from pull-request jobs
  - GitHub-hosted runners only, which are fresh for every job. A self-hosted or long-lived runner would let a malicious package stay behind and tamper with later runs
  - only the `pull_request` trigger for jobs that install unapproved dependencies. Triggers such as `pull_request_target` or `workflow_run` run with the main repository's secrets and a token that can write

  If any of this changes, this decision must be revisited. A malicious package would then have something to steal, or a way to poison later builds (T-603).
- **Other checks** narrow the gap without closing it:
  - the 7-day cooldown and Socket's report on the PR (T-601)
  - no install scripts (T-602)
  - the human's approval before merge

**What remains.** A malicious package can still run in CI and see the repository's source code, which is public here anyway. The design limits the damage to "a wasted CI run". It does not prevent the run.

### 5.7 TB6 — User-supplied files

| ID | STRIDE/P | Threat | Mitigation | Status | Evidence |
|---|---|---|---|---|---|
| T-701 | D, T | **Malformed or huge import files** (address lists, descriptors, future exchange CSVs) | Size/row limits; strict address validation (checksum, network); descriptors validated via `getdescriptorinfo` with a gap-limit cap; reject rather than guess; the import preview must be confirmed | Planned | |
| T-702 | T | **CSV/formula injection in exports**: a label or chain-derived text starting with `= + - @` executes when the file is opened in a spreadsheet | Type-aware export: numeric columns are written as numbers, so negative gains stay intact; free-text columns (labels, descriptions, notes) have leading `= + - @`, tab and CR neutralised; unit test | Planned | |
| T-703 | I | **Private keys in imported descriptors**: an xprv/WIF descriptor would be sent to the node over RPC (`scanblocks`, `getdescriptoractivity` and `deriveaddresses` all accept private material), could reach logs or error paths, and would be stored in the DB | Import rejects any descriptor or key containing private material, before it is stored, sent or logged, and tells the user how to export the public form. The redaction filter also masks xprv/WIF patterns. Unit and E2E tests cover it | Planned | |

---

## 6. Allowed network flows (exhaustive)

**Runtime:**

| Flow | From | To | Content | Notes |
|---|---|---|---|---|
| F1 | Browser | Backend `127.0.0.1:<port>` | UI/API | Bearer session from the bootstrap file; no cookies |
| F2 | Backend | Bitcoin Core JSON-RPC on **loopback only** | Whitelisted read-only chain queries | T-202, T-203 |
| F3 | Backend (`prices/` only) | Configured price/FX hosts, optionally via SOCKS5 | Bulk historical price/FX download | Manual trigger, date-independent (T-301) |

**Any other runtime flow is a bug.** New flows need an ADR and an update to this table.

**Build/dev time only** (never at runtime): package registries (npm, PyPI), **Socket Firewall** (sfw-free sends the names and versions of packages being installed to Socket), the Socket GitHub App, GitHub/CI, **OSV** (`make audit` sends the lockfiles' package names and versions to `api.osv.dev`; ADR 0027), toolchain and test downloads (`sfw`, pnpm, uv, Node.js, Python, `bitcoind`, Playwright browsers, actionlint, zizmor, osv-scanner), and zizmor's online audits (`make lint-workflows` → GitHub API, sending the action names and refs our workflows use, with a no-permission token; ADR 0026). Each is covered by §5.6 and ENGINEERING §2.

## 7. Verification plan (how threats become *Verified*)

- **Network (T-106, T-301, T-305, T-604):** the socket guard runs across the whole test suite. The E2E run performs the whole flow against regtest, from descriptor import through the 8949 export, and asserts that no unexpected connections or DNS lookups happened. A bundle scan checks for external URLs. A test checks that price requests are the same regardless of the user DB.
- **HTTP hardening (T-101–T-110):** integration tests send a forged Host, a missing or wrong bearer session token, a cross-origin request, and a reused or expired bootstrap token, and check the CSP, cache and referrer headers.
- **Node (T-202, T-203, T-206):** the regtest node is configured with and without `rpcwhitelist`; a non-loopback endpoint is refused; a pruned regtest node is refused; a wrong-chain DB is refused.
- **Chain access (T-205, T-207, T-209, T-210, T-212):** property/fuzz tests on scan-result processing; a regtest reorg (`invalidateblock` in the test harness) invalidates the affected cache rows; the node is refused when an index is missing, still syncing, or pruned; scans are aborted and cover the full range; the node-datadir trace check.
- **Storage (T-401–T-408):**
  - the DB is refused on a non-encrypted path, on Linux and macOS
  - side files and temp files are asserted to live on the volume path
  - file modes are checked
  - the log redaction corpus is checked
  - core dump settings are checked
  - the dismount watchdog makes the backend exit
  - change-log entries are recorded for edits
- **Tax (T-501–T-503, T-506, T-508–T-510):**
  - a hand-worked scenario suite per tax year, covering gifts, late identification, the 2025 allocation, withdrawals and fee roles
  - mutation testing
  - a determinism test (same inputs → byte-identical report)
  - box selection per year
- **Doxx (T-504, T-511):** hand-built graphs cover every rule, including the mixing exception.
- **Imports/exports (T-701, T-702):** malformed inputs are rejected; the formula-escaping and numeric-column tests pass.
- **Supply chain (T-601–T-603):** CI checks for the lockfile, cooldown config, disabled scripts, SHA-pinned actions, verified `bitcoind` downloads, and the Socket report.

## 8. Assumptions & out of scope

- **In scope for protection, but not app-enforced:** VeraCrypt is correctly configured by the user (strong passphrase, volume dismounted when not in use).
- **Trusted:**
  - the user's Bitcoin Core node, for consensus-valid chain data, **and its indexes** (`txindex`, `blockfilterindex`, `txospenderindex`). These are not consensus-validated. Filter-index corruption would show up as silently skipped ranges (T-210 residual)
  - the OS, kernel, browser engine and hardware (AD10)
- **Out of scope:**
  - malware running as the same OS user, or root, while the volume is mounted (it can read everything the app can)
  - physical coercion (AD11)
  - privacy of the Bitcoin Core node's own P2P behaviour; the app never broadcasts
  - **personalized tax advice** and the unsupported scenarios (lost/stolen coins, forks/airdrops, state taxes). Correctness of the supported federal rules *is* in scope (§5.5)

## 9. Accepted risks

| ID | Risk | Rationale |
|---|---|---|
| R-1 | While the volume is mounted, a same-user process can read all data | Mitigating it needs OS-level sandboxing beyond v1 scope; documented |
| R-2 | The price source operator sees one bulk download per refresh from the user's IP (if Tor is not used) | Content reveals nothing user-specific; Tor is offered |
| R-3 | The doxx model is a lower bound on what adversaries know (T-505) | No local tool can model proprietary chain analysis or off-chain data; mitigated by honest UI wording |
| R-4 | **In-process egress:** malicious code running inside the backend (e.g. a compromised dependency) could send user data to the internet. Nothing at the OS level blocks it | Decided 2026-09-27: OS-level sandboxing (bwrap/nftables on Linux, sandbox-exec/pf on macOS) adds cross-platform complexity and compatibility risk. Supply-chain controls (T-601–T-604) are the primary defence; the test-time socket guard catches accidental egress only. To be revisited if the dependency count grows or a sandbox becomes portable |
| R-5 | **Browser-side disk leakage:** the user's own browser may keep app content on plain disk despite `no-store` (session restore, crash reports, OS-level caches, history of the loopback URL) | User decision, 2026-09-27: v1 doesn't manage a browser profile. Launching specific browsers with a profile on the volume, or packaging a desktop shell (Electron, Tauri, pywebview), added complexity and supply-chain surface. The docs mention private windows as an optional precaution. To be revisited in a future version, via an ADR |
| R-6 | **Dev work and real data on the same machine:** an AI agent or a compromised dev tool can leave code in the working tree (venv, `node_modules`, built bundle, git hooks, Makefile). That code runs later when the user starts the app against real data | User decision, 2026-09-27: no technical dev/live separation. The user docs warn against doing development work, or running AI coding agents, on a machine where real financial data is used. Related: T-607 |
| R-7 | **Browser extensions can read the app's pages** (T-111) | This follows from the decision to use the user's default browser (R-5): a managed profile or desktop shell would have had no extensions. It is mitigated by docs advice (private window or clean profile). To be revisited together with R-5 |
| R-8 | **Deliberate evasion of the install-command guard** by an agent or a compromised dev tool (exported variables, `eval`, commands in script files, planted makefiles, unlisted launchers; list in #29) | User decision, 2026-09-28 (ADR 0022): the guard is hygiene against accidental installs. A command-text matcher can't stop a determined agent. Covered by human PR review, the no-real-data rule (T-607) and Socket Firewall on the actual installs (T-601) |
| R-9 | **AI review panel** (ADR 0020, ADR 0023): reviewers can read local files, and their reports are posted publicly; the panel runs the owner's PR code locally (tests, `make check`), as in normal development (R-6); a PR could be written to steer the AI reviewers and the Opus tripwire; the tripwire is a heuristic a deliberate author can evade, and its commit status is advisory; the orchestrator holds the owner's credentials, so "never merges" is procedural | User decisions, 2026-09-28 (ADR 0020) and 2026-09-29 (ADR 0023): AI agents are not sandboxed, because that is a very hard engineering programme; findings that an agent could be steered, or that a rule is only an instruction, are accepted here rather than fixed. **Assumptions:** the owner is the only one who can push (the owner-only commit check uses email-derived logins); the owner never keeps real data, or mounts VeraCrypt, on the machine where agents and reviewers run, so the panel has no data-volume checks (R-6, T-607 still apply to other agents). Limited by: the human still merges every PR, pointed at the tripwire flags (the mechanical scan runs from `main`'s copy with a fixed git configuration, and flags deletions as well as additions); reviewers can't write (read-only tools, Codex read-only sandbox); the mechanical secret scan (`scripts/secret_scan.py`) on everything posted; owner-only PRs and commits; symlinks and submodules are banned from the tree (`scripts/check_repo_files.py`, CI). Merge only the SHA named in the hand-off |

## 10. Open questions

1. ~~Non-Linux VeraCrypt detection (macOS/Windows) for T-401: do we support these platforms in v1?~~ **Resolved (2026-09-27):** v1 supports Linux and macOS; Windows is not planned. The macOS detection method is designed in M0 (T-401).
2. ~~**In-app "lock"** so the volume can be dismounted cleanly without killing the app.~~ **Resolved (2026-09-27): deferred to a future version.** For v1 the documented workflow is to quit the app and close its browser window, then dismount (T-405). The backend should still treat "no DB open" as a clean state where cheap, so a lock is easy to add later.
3. ~~Should a second price source be added for cross-checking (T-303)?~~ **Resolved (2026-09-27): deferred to a future version.** v1 relies on TLS, sanity checks, content hashes and user overrides (T-303). A second source would add one more outbound flow (§6), so it needs an ADR.
4. ~~**Minimum Bitcoin Core version**~~ **Resolved (2026-09-27): ≥ 31.0**, for `txospenderindex` (raised from 29.0 after the PR #4 reviews). The user accepts any minimum.

## 11. Changelog

| Date | Version | Change |
|---|---|---|
| 2026-09-26 | 0.1 | Initial design-stage threat model (all mitigations *Planned*) |
| 2026-09-27 | 0.2 | Node access is JSON-RPC only; REST (`rest=1`) is no longer used (T-203, F2, DFD) |
| 2026-09-27 | 0.3 | Incorporates the Fable 5.1 + Codex (gpt-5.6-sol) reviews and the user's decisions: loopback-only node with rpcauth + `rpcwhitelist` canary (T-201–T-203); pruned-node, deep-reorg and index-integrity threats (T-207, T-210, T-211); one-time fragment launch token, CSRF header as the primary control, stricter CSP, browser profile on volume, clipboard (T-101–T-110); per-platform VeraCrypt detection (T-401); tax correctness brought into scope with new threats T-508–T-511; AI-agent data exfiltration (T-607, AD12); type-aware CSV escaping (T-702); in-process egress accepted (R-4); new `Documented` status; Linux + macOS scope |
| 2026-09-27 | 0.3.1 | In-app lock deferred to a future version; v1 dismount workflow documented (T-405, §10) |
| 2026-09-27 | 0.3.2 | Second price source for cross-checking deferred to a future version (T-303, §10) |
| 2026-09-27 | 0.4 | No app-side chain index: chain data comes from Core's `txindex` + `blockfilterindex` via `scanblocks` and is cached in the user DB. Nothing the app writes lives on plain disk. A8, T-208, T-211 retired; T-205, T-207, T-209, T-210 rewritten; T-212 added (long scans); whitelist adds `gettxout`, `scanblocks`; Rust/crates removed from supply chain |
| 2026-09-27 | 0.4.1 | After source-level research on BIP158 and Core's `scanblocks`: silent-skip guard added to T-210; whitelist adds `getdescriptoractivity`, `gettxspendingprevout`; minimum Core version ≥ 29.0 agreed (§10.4) |
| 2026-09-27 | 0.4.2 | PR #4 review fixes (Opus 5.5, Codex): the `scanblocks` silent-skip guard is replaced by a scan protocol (the `to_height` postcheck was ineffective); Core ≥ 31.0 with `txospenderindex` for spender lookups; fork-point reorg handling covers negative results and coverage (T-207); BIP30 keyed by block (T-208); canary `debug.log` trace (T-209); busy-script budgets (T-205); orphaned scans (T-212); private keys in descriptors (new T-703); node indexes named as trusted (§8) |
| 2026-09-27 | 0.5 | Supply-chain rows updated for ENGINEERING v0.2 (PR #3): vet-before-install, lockfile policy check, load-time execution paths (T-601–T-603); only-human-merges is procedural (T-605); commit signing dropped (T-606, Accepted); sfw telemetry and toolchain downloads added to build-time flows (§6); R-6 dev work and real data on the same machine accepted |
| 2026-09-27 | 0.5.1 | No managed browser profile in v1: T-105 is split into app-side controls (Planned) and browser-side leakage (Accepted, R-5 widened) |
| 2026-09-27 | 0.6 | Architecture review fixes (PR #5): DFD matches architecture §1 (launcher, services → chain → rpc, pure engines, no API→node edge); bootstrap-file launch and cookie-less bearer session (T-102, T-103, T-110); download via POST blob (T-108); browser extensions (new T-111, R-7); unencrypted storage only off mainnet, data dir never saved (T-401); redaction covers uvicorn and exception hooks (T-403) |
| 2026-09-27 | 0.6.1 | T-508: a late lot identification is warned about and recorded, not overridden, and doesn't block reports (user decision) |
| 2026-09-27 | 0.6.2 | PR #6 review fixes: T-508 covers exchange withdrawals and self-custody (standing order before FIFO; merged UTXOs oldest-first); T-406 no default data dir; T-605 evidence = AGENTS.md + ADR 0018; §7 tests use the bearer session |
| 2026-09-28 | 0.7 | M0.1: first implemented mitigations: toolchain hash verification and a SHA-pinned CI action (T-603, partly); the Claude Code guard hook (T-605, T-607) |
| 2026-09-28 | 0.7.1 | PR #7 review round 1: T-603/T-607 rows restored with their mitigations (the 0.7 edit had dropped them) and the hook's limits stated; the Socket Firewall path hardened (no make overrides, the pinned binary is hash-verified, strict guard for make, `uv run` and other implicit installs banned, `UV_NO_SYNC` for agents) |
| 2026-09-28 | 0.7.2 | PR #7 review round 2: installer detection tokenizes commands and uses per-tool subcommand allowlists; the make guard sees through wrappers, `VAR=` prefixes and `cd`; sfw, pnpm and uv are verified before every install with a non-overridable verifier (T-601, T-603) |
| 2026-09-28 | 0.7.3 | The install-command guard is hygiene against accidental installs (ADR 0022); deliberate evasion accepted as R-8. The 0.7.2 wording "sees through wrappers" means common wrappers only. pnpm now comes from the npm registry tarball (sha512 integrity) |
| 2026-09-28 | 0.7.4 | PR #7 review round 4: `bootstrap` refuses unmerged dependency changes unless the human approves (T-601); toolchain verification covers every installed file, including pnpm's code and Node, and a moved checkout (T-603) |
| 2026-09-28 | 0.7.5 | PR #7 review round 5: the approval gate also covers toolchain pins (`make toolchain`/`test-tools`) and install-affecting config, and accepts approval only from the make command line (T-601, T-603); the pinned Python is verified before installs, bytecode included (T-603) |
| 2026-09-28 | 0.7.6 | T-608 and §5.6.1: the approval chicken-and-egg explained in plain language; unapproved dependencies run only in CI, which passes approval explicitly (user decision, #44) |
| 2026-09-28 | 0.7.7 | PR #7 review round 6: approval gate hardened (full ref, ignored files, installer checks its own pins, propose-time config gate, `--no-build`, `ignorePnpmfile`) (T-601, T-602); limits stated: `MAKEFLAGS`, untrusted branches, marker-stored digest (T-603) |
| 2026-09-28 | 0.7.8 | PR #7 review round 7: `DEPS_APPROVED` for agents is a written rule outside Claude Code (T-601, §5.6.1); §5.6.1/T-608 add the GitHub-hosted-runner and `pull_request`-only conditions |
| 2026-09-28 | 0.7.9 | T-508: automatic standing-method mode; the late flag is set only when the choice differs (ADR 0021) |
| 2026-09-28 | 0.7.10 | T-605: the review panel's tripwire focuses the human's merge review (ADR 0020); new accepted risk R-9 (AI review panel), including unsandboxed AI agents (user decision 2026-09-29) |
| 2026-09-29 | 0.7.11 | ADR 0023: R-9 drops the panel's data-volume clause (owner practice stated as an assumption, with the sole-pusher assumption); symlinks and submodules banned by a CI check |
| 2026-09-30 | 0.7.12 | T-603: installed toolchain trees are read-only. Found at M0.2 setup: running the pinned Python wrote `.pyc` files into its own tree, which failed verification. Leaving `.pyc` files out of the digest would have let a planted one run unnoticed |
| 2026-09-30 | 0.7.13 | T-603: pnpm is pinned as its native binary; the `pnpm` npm package is only a launcher that fetches the binary in an install script, or downloads one at run time (found at M0.2 setup) |
| 2026-09-30 | 0.7.14 | T-601, T-602: the lockfile policy check exists (`scripts/check_lockfiles.py`, in CI): `uv.lock` sources, hashes and age; no lifecycle scripts, `.pnpmfile` or `configDependencies`. The pnpm-lockfile part is still to come, so a pnpm lockfile fails the check. Both rows stay Planned until the rest of their mitigations exist |
| 2026-10-01 | 0.7.15 | T-602: the `.pth` allowlist check exists (`scripts/check_pth.py`, run by `make bootstrap` after every install; allowlist: uv's own `_virtualenv.pth`). At the first install (pytest, PR #65) no wheel shipped a `.pth` file. Status stays Planned until CI runs it (#44) |
| 2026-10-01 | 0.7.16 | T-602 (package managers auto-downloading): found at M0.2 setup that the pinned pnpm 12 resolved its `packageManager` pin from the registry on `pnpm --version`, outside `sfw`, and wrote a `pnpm-lock.yaml` (#51). The field is removed (the pin lives in `scripts/toolchain.lock`) and the lockfile check rejects it. Tested with the registry pointed at a dead port: with the field, pnpm hangs on the network; without it, it makes no request |
| 2026-10-01 | 0.7.17 | T-601: ADR 0024 (proposed) allows prebuilt native-code wheels for four development tools only (`hypothesis`, `coverage`, `ruff`, `mypy`); their compiled parts are less reviewable than Python source. Any other native-code dependency, runtime ones included, still needs its own ADR |
| 2026-10-01 | 0.7.18 | T-601/T-602: `coverage` 7.16.1 proposed as a development dependency (PR #78; native C tracer, allowed by ADR 0024). Its wheels ship a start-up hook, `a1_coverage.pth`, which imports coverage only when `COVERAGE_PROCESS_START`/`COVERAGE_PROCESS_CONFIG` is set. ADR 0025 adds it to the `.pth` allowlist, pinned by sha256 and by coverage's own `RECORD` claim; tests use the real file. Residual: a process started with those variables measures itself (R-6: never for a real-data run) |
| 2026-10-01 | 0.7.19 | T-601, T-602: the lockfile check closes the #71/#72 gaps. Each `uv.lock` file must match its entry's name and version. Escaped `configDependencies`/`packageManager` keys, hook-file and per-branch/per-package lockfile settings, the `lockfile`/`lockfileDir` settings, indirect YAML key syntax (explicit keys, tags, anchors, aliases, document markers), a file URL not of PyPI's exact shape (query, fragment, `%`, backslash, dot segment), and a missing or repeated `ignorePnpmfile: true`, and non-ASCII text or a byte-order mark outside comments all fail, as do `.pnpmfile` in any case and any pnpm lockfile in the tree in any letter case. The wrapper and `make check` run the pinned Python only once a host `python3`, never the pinned one, has verified it. The upload-time limit is accepted (PR #63) and recorded in ENGINEERING §2.5. Statuses stay Planned |
| 2026-10-01 | 0.7.20 | T-603: actionlint and zizmor are pinned toolchain binaries (ADR 0026), hash-pinned, trust-on-first-use against GitHub (the provenance listing isn't verified yet). zizmor runs its online audits (impostor-commit, known-vulnerable-actions, ref-confusion, ref-version-mismatch, stale-action-refs, typosquat-uses) with `--no-config`; `check_repo_files` rejects linter config and zizmor ignore comments: a new dev-time flow to the GitHub API (§6) with a dedicated no-permission token; both tools run with an otherwise empty environment, so neither sees the caller's tokens. actionlint runs no unpinned helpers. Architecture §9 shows the flow (0.2.2). CI runs them once the pins are merged (#94). Status stays Implemented (partly) |
| 2026-10-01 | 0.7.21 | T-603 (#69): verification now also fails on a writable toolchain tree (`make toolchain` re-hardens it). `remove_tree` refuses a symlinked tool directory before any chmod. A failed hardening leaves no valid marker. Limits (root, owner `chmod`) are recorded in ENGINEERING §2.3; PR #83 review: tool paths with a symlinked component (`.toolchain`, `.toolchain/bin`, `.toolchain/<name>`, the version directory) are refused, and an unreadable directory fails the digest and write-bit checks instead of being skipped |
| 2026-10-01 | 0.7.22 | §6, T-601 and T-603 (ADR 0027): `make audit` uses a pinned `osv-scanner`, trust-on-first-use against GitHub. It adds a dev-time flow to `api.osv.dev` carrying the local lockfiles' package names and versions (for merged lockfiles, what the public repository already shows) and the requester's IP address. It runs with an empty config from outside the repository (a committed `osv-scanner.toml` is also rejected), `--no-resolve` (no deps.dev or registry lookups) and an empty environment. Statuses stay Planned |
| 2026-10-01 | 0.7.23 | T-603 (#67): the pinned pnpm 12 is a Rust binary with no embedded Node runtime (no V8 or Node symbols), so it doesn't depend on the pinned Node. The unused `npm-tgz` install path and its Node chaining are removed, and the shipped pin type (`tar` plus sha512 integrity) is now tested. The pnpm packages carry an npm registry signature and a SLSA provenance attestation, not yet verified at install (#15) |
| 2026-10-02 | 0.7.24 | M0.3 RPC client: T-201, T-202, T-203, T-206, T-209, T-210 move to Implemented (partly) with evidence (config parsing, `rpc.py`, startup node checks, unit tests); T-305 Implemented (partly): the socket guard, on before collection, not yet in CI |
| 2026-10-02 | 0.7.25 | T-203, T-209, T-210: regtest integration tests (harness `e2e/harness/regtest.py`); T-209's M0 datadir check is done, descriptor scans included, with and without `debug=rpc,http` |
| 2026-10-02 | 0.7.26 | M0.3 storage checks: T-401 (Linux detection, macOS confirmation fallback, the unencrypted-storage policy), T-402 (`<data>/tmp`) and T-406 (no data dir inside a git working tree) move to Implemented (partly) |
| 2026-10-02 | 0.7.27 | M0.3 launcher (before the server): T-404 Implemented †; T-110, T-401, T-402, T-403 extended (bootstrap file, start-up refusal, temp dirs, redacting log handler and exception hooks) |
| 2026-10-02 | 0.7.28 | T-405: the dismount watchdog (Implemented, partly) |
