"""Tags the user sets from the graph, and the change log (PLAN §2, §3, §4; THREAT_MODEL T-408, T-504).

- `tx_flag` holds the **mixing** flag of a transaction (CoinJoin/PayJoin-like). The common-input
  heuristic and doxx propagation treat a mixing transaction differently (PLAN §3, §5), so the flag
  is the user's to set or clear. `source` says whether the user set it or the app suggested it.
- `change_log` is the **append-only** record of every edit to tags (and, later, events,
  identifications and overrides): what changed, from what, to what, and when. The triggers below
  refuse any update or delete, so a row can only ever be added (T-408).
"""

SQL = """
CREATE TABLE tx_flag (
    txid TEXT NOT NULL PRIMARY KEY CHECK (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*'),
    mixing INTEGER NOT NULL CHECK (mixing IN (0, 1)),
    source TEXT NOT NULL CHECK (source IN ('user', 'auto'))
) STRICT, WITHOUT ROWID;

CREATE TABLE change_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL CHECK (length(at) BETWEEN 20 AND 40),
    kind TEXT NOT NULL CHECK (kind IN ('address_tag', 'tx_flag')),
    subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 20000),
    before TEXT CHECK (before IS NULL OR json_valid(before)),
    after TEXT NOT NULL CHECK (json_valid(after))
) STRICT;

CREATE TRIGGER change_log_is_append_only_update BEFORE UPDATE ON change_log
BEGIN
    SELECT RAISE(ABORT, 'the change log is append-only');
END;
CREATE TRIGGER change_log_is_append_only_delete BEFORE DELETE ON change_log
BEGIN
    SELECT RAISE(ABORT, 'the change log is append-only');
END;
"""
