"""Migration 9: a change log with an id below 1 is refused, and m0007's guard compares only real ids
(THREAT_MODEL T-408).

m0007's BEFORE INSERT trigger compares `NEW.id` with the stored ids, but for an insert that leaves the
id to SQLite `NEW.id` is undefined there (https://www.sqlite.org/lang_createtrigger.html; in practice
-1). m0008 refuses new ids below 1, but a DB that stored one before m0008 would still go wrong: a
stored -1 matches every later append, and with no positive id stored SQLite picks the next id as the
largest plus one, which m0008 then refuses.

The app never writes an id itself, so such a row means the log was edited outside the app. The log
can't be repaired without rewriting it, so this step fails closed: its CHECK
(`change_log_ids_start_at_1`) refuses the upgrade, the step rolls back, the DB stays at version 8 and
the app won't open it (T-408: restore it from a backup). Otherwise it recreates m0007's guard to
compare only ids of 1 or more, so an undefined `NEW.id` can never match a row.
"""

SQL = """
CREATE TEMP TABLE m0009_check (
    below_1 INTEGER NOT NULL CONSTRAINT change_log_ids_start_at_1 CHECK (below_1 = 0)
);
INSERT INTO m0009_check SELECT count(*) FROM change_log WHERE id < 1;
DROP TABLE m0009_check;
DROP TRIGGER change_log_no_replace;
CREATE TRIGGER change_log_no_replace BEFORE INSERT ON change_log
WHEN NEW.id >= 1 AND EXISTS (SELECT 1 FROM change_log WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'the change log is append-only'); END;
"""
