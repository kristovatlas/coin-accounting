"""The user DB's schema steps, in order (architecture §2, `storage/migrations/`).

Each step is a module `mNNNN_name.py` with an `SQL` string, listed here by plain import: no file
discovery and no dynamic imports. `storage.db` applies them in order and records the last one in
`PRAGMA user_version`. A released step is never edited; a change is a new step. Steps run with
foreign keys off and are checked with `foreign_key_check` before they commit.
"""

from typing import Final

from coinacct.storage.migrations import (
    m0001_chain_state,
    m0002_chain_cache,
    m0003_scan_marker,
    m0004_review_queue,
    m0005_accounts,
    m0006_tags,
    m0007_change_log_ids,
    m0008_change_log_positive_ids,
    m0009_change_log_no_replace,
    m0010_change_log_origin,
)

STEPS: Final[tuple[tuple[int, str], ...]] = (
    (1, m0001_chain_state.SQL),
    (2, m0002_chain_cache.SQL),
    (3, m0003_scan_marker.SQL),
    (4, m0004_review_queue.SQL),
    (5, m0005_accounts.SQL),
    (6, m0006_tags.SQL),
    (7, m0007_change_log_ids.SQL),
    (8, m0008_change_log_positive_ids.SQL),
    (9, m0009_change_log_no_replace.SQL),
    (10, m0010_change_log_origin.SQL),
)
