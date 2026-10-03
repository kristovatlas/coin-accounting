"""The log file handler (architecture §1, §6; THREAT_MODEL T-403).

Logs go only to `<data>/logs/coinacct.log`: a regular file of mode 0600 on the verified device,
never opened through a link. Every record is redacted after formatting, so the message, its
arguments and any traceback are all covered. A record that fails to format or write is reported
to stderr with a fixed line only: logging's default error path would print it unredacted. The
launcher installs this once; no module imports it (architecture §2, "Logging").
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import sys
from typing import Final

from coinacct.domain.redact import redact
from coinacct.storage.datadir import DataDir, DataDirError, require_device

LOG_NAME: Final = "coinacct.log"
FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactingFileHandler(logging.StreamHandler):  # type: ignore[type-arg]
    def handleError(self, record: logging.LogRecord) -> None:
        # Never logging's default, which prints the record's raw message and arguments.
        kind = sys.exc_info()[0]
        name = kind.__name__ if kind else "error"
        sys.stderr.write(f"coinacct: a log record could not be written ({name}); see T-403\n")


def open_log_handler(data_dir: DataDir) -> RedactingFileHandler:
    # Not data_dir.path(): that resolves links, and a linked log file must fail to open instead.
    # O_NONBLOCK so a FIFO there can't block start-up before the type check below.
    path = data_dir.root / "logs" / LOG_NAME
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as e:
        if e.errno == errno.ENXIO:  # a FIFO with no reader
            raise PermissionError(f"{path} must be a regular file (T-403)") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"{path} must be a regular file (T-403)")
        require_device(path, st, data_dir.device)
        if st.st_mode & 0o077 or st.st_uid != os.getuid():
            raise PermissionError(f"{path} must be the user's own file with mode 600 (T-403)")
        os.set_blocking(fd, True)
    except (OSError, DataDirError):
        os.close(fd)
        raise
    handler = RedactingFileHandler(os.fdopen(fd, "a", encoding="utf-8"))
    handler.setFormatter(RedactingFormatter(FORMAT))
    return handler
