"""The user DB's schema steps, in order (architecture §2, `storage/migrations/`).

Each step is a module `mNNNN_name.py` with an `SQL` string, listed here by plain import: no file
discovery and no dynamic imports. `storage.db` applies them in order and records the last one in
`PRAGMA user_version`. A released step is never edited; a change is a new step. Steps run with
foreign keys off and are checked with `foreign_key_check` before they commit.
"""

from typing import Final

from coinacct.storage.migrations import m0001_chain_state, m0002_chain_cache

STEPS: Final[tuple[tuple[int, str], ...]] = (
    (1, m0001_chain_state.SQL),
    (2, m0002_chain_cache.SQL),
)
