"""Migration 10: each change-log row records what made the change (THREAT_MODEL T-504, T-408; #229).

`origin` is one of:
- `user`: the user, through the app (a tag or a flag set from the graph, an accepted suggestion's
  confirmation is `suggestion`)
- `import`, `descriptor`, `discovery`: an address list import, a descriptor import, or discovery
- `heuristic`: a flag the app set by itself, which T-504 allows only for a transaction's mixing flag
  (M4). The CHECK refuses it on any other kind of row: clustering and every other heuristic only suggest
  (ADR 0010, PLAN §3), so their changes reach the log as `suggestion`, once the user accepts them.
- `suggestion`: a heuristic's suggestion the user accepted (M4)

Rows logged before this step have no origin (NULL: not recorded). From this step on, every new row must
name one: the trigger refuses an insert without it.
"""

SQL = """
ALTER TABLE change_log ADD COLUMN origin TEXT CHECK (
    origin IS NULL OR (
        origin IN ('user', 'import', 'descriptor', 'discovery', 'heuristic', 'suggestion')
        AND (origin <> 'heuristic' OR kind = 'tx_flag')
    )
);
CREATE TRIGGER change_log_names_its_origin BEFORE INSERT ON change_log
WHEN NEW.origin IS NULL
BEGIN SELECT RAISE(ABORT, 'a change-log row names its origin'); END;
"""
