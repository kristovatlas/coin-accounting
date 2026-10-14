"""Migration 12: the user's settings (PLAN §2, architecture §6; THREAT_MODEL T-503).

`setting` holds one row per setting the user has changed; a setting without a row has its default
(`storage.settings`). The CHECK lists the settings there are, so an unknown one is refused. The only
one so far is `time_zone`: the IANA zone an event's tax date is taken in (PLAN §7).

A change isn't in the change log, whose kinds PLAN §2 lists; tax-affecting settings are embedded in
each report and its input hash instead (PLAN §8, T-503).
"""

SQL = """
CREATE TABLE setting (
    key TEXT PRIMARY KEY CHECK (key IN ('time_zone')),
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 64)
) STRICT;
"""
