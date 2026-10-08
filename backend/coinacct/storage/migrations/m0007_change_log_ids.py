"""Migration 7: no insert replaces a logged row (THREAT_MODEL T-408).

m0006's triggers refuse an UPDATE or DELETE of a logged row. `INSERT OR REPLACE` (or `REPLACE INTO`)
with an existing id would still replace one: REPLACE deletes the old row without firing delete
triggers while `recursive_triggers` is off (https://www.sqlite.org/lang_conflict.html). This trigger
runs before the conflict is resolved and refuses an insert whose id is taken. (For an insert that
leaves the id to SQLite, `NEW.id` is undefined here, in practice -1; m0009 compares only ids of 1
or more.)
"""

SQL = """
CREATE TRIGGER change_log_no_replace BEFORE INSERT ON change_log
WHEN EXISTS (SELECT 1 FROM change_log WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'the change log is append-only'); END;
"""
