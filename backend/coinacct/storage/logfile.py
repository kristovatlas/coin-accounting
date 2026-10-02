"""The log file handler (architecture §1, §6; THREAT_MODEL T-403).

Logs go only to `<data>/logs/coinacct.log` (mode 0600, never through a link). Every record is
redacted after formatting, so the message, its arguments and any traceback are all covered. The
launcher installs this once; no module imports it (architecture §2, "Logging").
"""

from __future__ import annotations

import logging
import os
from typing import Final

from coinacct.domain.redact import redact
from coinacct.storage.datadir import DataDir

LOG_NAME: Final = "coinacct.log"
FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def open_log_handler(data_dir: DataDir) -> logging.Handler:
    # Not data_dir.path(): that resolves links, and a linked log file must fail to open instead.
    path = data_dir.root / "logs" / LOG_NAME
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if st.st_mode & 0o077 or st.st_uid != os.getuid():
        os.close(fd)
        raise PermissionError(f"{path} must be the user's own file with mode 600 (T-403)")
    handler = logging.StreamHandler(os.fdopen(fd, "a", encoding="utf-8"))
    handler.setFormatter(RedactingFormatter(FORMAT))
    return handler
