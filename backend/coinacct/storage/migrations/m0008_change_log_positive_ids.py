"""Migration 8: every change-log id is 1 or more (THREAT_MODEL T-408).

m0007's trigger compares `NEW.id` with the stored ids. For an insert that leaves the id to SQLite,
`NEW.id` is undefined in a BEFORE trigger (https://www.sqlite.org/lang_createtrigger.html; in practice
-1), so a row stored with id -1 would make every later append fail. In an AFTER trigger the id is
real: this one refuses any id below 1, so no stored id can match an undefined one.
"""

SQL = """
CREATE TRIGGER change_log_ids_are_positive AFTER INSERT ON change_log
WHEN NEW.id < 1
BEGIN SELECT RAISE(ABORT, 'the change log is append-only'); END;
"""
