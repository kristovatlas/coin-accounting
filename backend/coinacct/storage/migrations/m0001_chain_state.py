"""The chain this data directory belongs to, and the last tip the app saw (PLAN §1, §2; T-206, T-207).

One row. The chain never changes once recorded: a node on another chain means offline mode.
"""

SQL = """
CREATE TABLE chain_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    chain TEXT NOT NULL CHECK (chain IN ('main', 'test', 'testnet4', 'signet', 'regtest')),
    tip_hash TEXT CHECK (tip_hash IS NULL OR (length(tip_hash) = 64 AND tip_hash NOT GLOB '*[^0-9a-f]*')),
    tip_height INTEGER CHECK (tip_height IS NULL OR tip_height >= 0),
    CHECK ((tip_hash IS NULL) = (tip_height IS NULL))
) STRICT;
"""
