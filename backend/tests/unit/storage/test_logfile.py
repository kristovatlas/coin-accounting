"""The log file handler (THREAT_MODEL T-403)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from coinacct.storage.datadir import DataDir, open_data_dir
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
