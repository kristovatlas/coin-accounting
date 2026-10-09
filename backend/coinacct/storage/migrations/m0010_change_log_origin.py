"""Migration 10: each change-log row records what made the change (THREAT_MODEL T-504, T-408; #229).

`origin` is one of:
- `user`: the user, through the app (a tag or a flag set from the graph, an accepted suggestion's
  confirmation is `suggestion`)
- `import`, `descriptor`, `discovery`: an address list import, a descriptor import, or discovery
- `heuristic`: a rule the app applied by itself (M4: auto-detected mixing, clustering)
- `suggestion`: a heuristic's suggestion the user accepted (M4)

Rows logged before this step have no origin (NULL: not recorded). From this step on, every new row must
name one: the trigger refuses an insert without it.
"""

SQL = """
ALTER TABLE change_log ADD COLUMN origin TEXT CHECK (
    origin IS NULL OR origin IN ('user', 'import', 'descriptor', 'discovery', 'heuristic', 'suggestion')
);
CREATE TRIGGER change_log_names_its_origin BEFORE INSERT ON change_log
WHEN NEW.origin IS NULL
BEGIN SELECT RAISE(ABORT, 'a change-log row names its origin'); END;
"""
