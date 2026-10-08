"""The chain-data cache: the node's answers, kept on the volume (PLAN §1 "Caching and chain state",
§2; THREAT_MODEL T-207, T-208).

Every block-backed row records the block hash and height it came from, so a reorg can drop exactly
the rows above the fork point. Negative answers are snapshots tagged with the tip they were computed
against. Mempool results are never stored here.
"""

SQL = """
-- #163: REPLACE deletes the old row without firing a delete trigger, so a second insert is refused
-- outright (T-206).
CREATE TRIGGER chain_state_is_inserted_once BEFORE INSERT ON chain_state
WHEN EXISTS (SELECT 1 FROM chain_state)
BEGIN
    SELECT RAISE(ABORT, 'the recorded chain never changes');
END;

-- The tip a catch-up is bringing the cache up to (architecture §8): rows are written against it, and
-- the last-seen tip moves to it only once coverage has been extended, so a failed scan is retried.
ALTER TABLE chain_state ADD COLUMN target_hash TEXT CHECK (
    target_hash IS NULL OR (length(target_hash) = 64 AND target_hash NOT GLOB '*[^0-9a-f]*')
);
ALTER TABLE chain_state ADD COLUMN target_height INTEGER CHECK (
    (target_height IS NULL) = (target_hash IS NULL) AND (target_height IS NULL OR target_height >= 0)
);

-- Decoded confirmed transactions, keyed by (txid, block) for BIP30's duplicate coinbases (T-208).
-- `data` holds the inputs and outputs; confirmations are worked out from the tip when read.
CREATE TABLE tx_cache (
    txid TEXT NOT NULL CHECK (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*'),
    blockhash TEXT NOT NULL CHECK (length(blockhash) = 64 AND blockhash NOT GLOB '*[^0-9a-f]*'),
    height INTEGER NOT NULL CHECK (height >= 0),
    data TEXT NOT NULL CHECK (json_valid(data)),
    PRIMARY KEY (txid, blockhash)
) STRICT, WITHOUT ROWID;
CREATE INDEX tx_cache_by_height ON tx_cache (height);

-- Confirmed spends: which transaction, in which block, spent an output (`gettxspendingprevout`).
CREATE TABLE spender (
    txid TEXT NOT NULL CHECK (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*'),
    vout INTEGER NOT NULL CHECK (vout >= 0),
    spend_txid TEXT NOT NULL CHECK (length(spend_txid) = 64 AND spend_txid NOT GLOB '*[^0-9a-f]*'),
    blockhash TEXT NOT NULL CHECK (length(blockhash) = 64 AND blockhash NOT GLOB '*[^0-9a-f]*'),
    height INTEGER NOT NULL CHECK (height >= 0),
    PRIMARY KEY (txid, vout)
) STRICT, WITHOUT ROWID;
CREATE INDEX spender_by_height ON spender (height);

-- Receive and spend events per script (`getdescriptoractivity`). `n` is the output index of a
-- receive or the input index of a spend; a spend also names the outpoint it spent.
CREATE TABLE activity (
    script_hex TEXT NOT NULL CHECK (length(script_hex) % 2 = 0 AND script_hex NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK (kind IN ('receive', 'spend')),
    txid TEXT NOT NULL CHECK (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*'),
    n INTEGER NOT NULL CHECK (n >= 0),
    sats INTEGER NOT NULL CHECK (sats BETWEEN 0 AND 2100000000000000),
    prevout_txid TEXT CHECK (
        prevout_txid IS NULL OR (length(prevout_txid) = 64 AND prevout_txid NOT GLOB '*[^0-9a-f]*')
    ),
    prevout_vout INTEGER CHECK (prevout_vout IS NULL OR prevout_vout >= 0),
    blockhash TEXT NOT NULL CHECK (length(blockhash) = 64 AND blockhash NOT GLOB '*[^0-9a-f]*'),
    height INTEGER NOT NULL CHECK (height >= 0),
    CHECK ((kind = 'spend') = (prevout_txid IS NOT NULL)),
    CHECK ((prevout_txid IS NULL) = (prevout_vout IS NULL)),
    PRIMARY KEY (kind, txid, n, blockhash)
) STRICT, WITHOUT ROWID;
CREATE INDEX activity_by_script ON activity (script_hex);
CREATE INDEX activity_by_height ON activity (height);

-- What has been scanned for each subject (a descriptor with its range, or a raw script): one
-- contiguous height range, and the hash of its stop block.
CREATE TABLE coverage (
    subject TEXT NOT NULL PRIMARY KEY CHECK (length(subject) > 0),
    start_height INTEGER NOT NULL CHECK (start_height >= 0),
    stop_height INTEGER NOT NULL CHECK (stop_height >= start_height),
    stop_hash TEXT NOT NULL CHECK (length(stop_hash) = 64 AND stop_hash NOT GLOB '*[^0-9a-f]*')
) STRICT, WITHOUT ROWID;

-- Negative answers, never facts: "this output was unspent at this tip". "No activity" for a script
-- needs no row: it is its coverage with no activity events in that range.
CREATE TABLE snapshot (
    kind TEXT NOT NULL CHECK (kind IN ('unspent')),
    txid TEXT NOT NULL CHECK (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*'),
    vout INTEGER NOT NULL CHECK (vout >= 0),
    tip_hash TEXT NOT NULL CHECK (length(tip_hash) = 64 AND tip_hash NOT GLOB '*[^0-9a-f]*'),
    tip_height INTEGER NOT NULL CHECK (tip_height >= 0),
    PRIMARY KEY (kind, txid, vout)
) STRICT, WITHOUT ROWID;
CREATE INDEX snapshot_by_height ON snapshot (tip_height);
"""
