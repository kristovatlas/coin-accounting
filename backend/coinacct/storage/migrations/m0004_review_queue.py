"""Transactions a reorg removed, waiting for review (architecture §8.4; THREAT_MODEL T-207, T-506).

`invalidate_above` records here, in its own transaction, every txid whose cached rows it deleted.
A txid stays until the events built on it have been flagged for review, so a crash or a failed sync
after the invalidation never loses the list.
"""

SQL = """
CREATE TABLE review_queue (
    txid TEXT NOT NULL PRIMARY KEY CHECK (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*')
) STRICT, WITHOUT ROWID;
"""
