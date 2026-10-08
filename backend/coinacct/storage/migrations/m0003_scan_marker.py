"""The in-flight scan marker (architecture §8.2, §3 shutdown; THREAT_MODEL T-212).

Set just before a `scanblocks` call and cleared once the node has answered it. A marker left behind
(a crash, a client timeout) means the app may have left a scan running on the node, which keeps
running after the client disconnects, so it is aborted at the next start-up.
"""

SQL = """
CREATE TABLE scan_marker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    subject TEXT NOT NULL CHECK (length(subject) > 0)
) STRICT;
"""
