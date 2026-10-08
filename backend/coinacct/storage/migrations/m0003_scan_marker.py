"""The in-flight scan marker, and each subject's candidate-block count (architecture §8.2, §3
shutdown; THREAT_MODEL T-205, T-212).

Set just before a `scanblocks` call and cleared once the node has answered it. A marker left behind
(a crash, a client timeout) means the app may have left a scan running on the node, which keeps
running after the client disconnects, so it is aborted at the next start-up.

The candidate count is the number of blocks `scanblocks` reported for a subject's coverage: the
busy-script budget (T-205) is checked against it, so retrying a scan doesn't reset the budget.
"""

SQL = """
CREATE TABLE scan_marker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    subject TEXT NOT NULL CHECK (length(subject) > 0)
) STRICT;

ALTER TABLE coverage ADD COLUMN candidates INTEGER NOT NULL DEFAULT 0 CHECK (candidates >= 0);
"""
