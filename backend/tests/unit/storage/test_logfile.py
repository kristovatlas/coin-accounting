"""The log file handler (THREAT_MODEL T-403)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest

from coinacct.storage.datadir import DataDir, DataDirError, open_data_dir
from coinacct.storage.logfile import LOG_NAME, open_log_handler

TXID = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


@pytest.fixture
def dd(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


def test_records_and_tracebacks_are_redacted_into_a_private_file_t403(dd: DataDir) -> None:
    handler = open_log_handler(dd)
    logger = logging.getLogger("test.logfile")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("spent %s for %s BTC", TXID, "0.50000000")
        try:
            raise KeyError(TXID)
        except KeyError:
            logger.exception("lookup failed")
    finally:
        logger.removeHandler(handler)
        handler.close()
    path = dd.root / "logs" / LOG_NAME
    text = path.read_text()
    assert TXID not in text and "0.50000000" not in text
    assert "spent <hex> for <amount> BTC" in text
    assert "KeyError: '<hex>'" in text
    # The timestamp is left alone: its `12:34:56,789` must not be masked as a grouped number.
    stamped = [line for line in text.splitlines() if " WARNING " in line or " ERROR " in line]
    assert len(stamped) == 2
    assert all(re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ", line) for line in stamped)
    assert path.stat().st_mode & 0o777 == 0o600


def test_an_existing_log_others_can_read_is_refused_t403(dd: DataDir) -> None:
    path = dd.root / "logs" / LOG_NAME
    path.write_text("")
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="mode 600"):
        open_log_handler(dd)


def test_a_symlinked_log_file_is_refused(dd: DataDir, tmp_path: Path) -> None:
    (dd.root / "logs" / LOG_NAME).symlink_to(tmp_path / "elsewhere.log")
    with pytest.raises(OSError):
        open_log_handler(dd)
    assert not (tmp_path / "elsewhere.log").exists()


def test_a_record_that_fails_never_reaches_stderr_unredacted_t403(
    dd: DataDir, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = open_log_handler(dd)
    logger = logging.getLogger("test.logfile.error")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("tx %d", TXID)  # wrong format: logging's default error path prints msg and args
        handler.stream.close()
        logger.warning("spent %s", TXID)  # write fails: the same path
    finally:
        logger.removeHandler(handler)
    err = capsys.readouterr().err
    assert TXID not in err
    assert err.count("coinacct: a log record could not be written") == 2


def test_a_fifo_log_is_refused_without_blocking_t403(dd: DataDir) -> None:
    fifo = dd.root / "logs" / LOG_NAME
    os.mkfifo(fifo, 0o600)
    with pytest.raises(PermissionError, match="regular file"):
        open_log_handler(dd)  # no reader: the open itself fails
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(PermissionError, match="regular file"):
            open_log_handler(dd)  # with a reader the open succeeds, and the type check refuses it
    finally:
        os.close(reader)


def test_a_log_on_another_filesystem_is_refused_t401(dd: DataDir, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd)[:10])
        fields[2] += 1  # st_dev: as for a bind-mounted log file
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(DataDirError, match="different filesystem"):
        open_log_handler(dd)
