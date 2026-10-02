"""Reading config.toml from the data directory (architecture §6; THREAT_MODEL T-201)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coinacct.storage.config_file import MAX_BYTES, ConfigFileError, read_config_text
from coinacct.storage.datadir import DataDir, open_data_dir


@pytest.fixture
def dd(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


def write(dd: DataDir, data: bytes, mode: int = 0o600) -> Path:
    path = dd.root / "config.toml"
    path.write_bytes(data)
    path.chmod(mode)
    return path


def test_a_private_config_is_read(dd: DataDir) -> None:
    write(dd, b"[rpc]\nport = 1\n")
    assert read_config_text(dd) == "[rpc]\nport = 1\n"


def test_a_missing_config_says_what_to_create(dd: DataDir) -> None:
    with pytest.raises(ConfigFileError, match="doesn't exist"):
        read_config_text(dd)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_config_others_can_read_is_refused_t201(dd: DataDir, mode: int) -> None:
    write(dd, b"[rpc]\n", mode)
    with pytest.raises(ConfigFileError, match="chmod 600"):
        read_config_text(dd)


def test_a_symlinked_config_is_refused(dd: DataDir, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.toml"
    target.write_text("[rpc]\n")
    target.chmod(0o600)
    (dd.root / "config.toml").symlink_to(target)
    with pytest.raises(ConfigFileError, match="not a link"):
        read_config_text(dd)


def test_a_directory_named_config_is_refused(dd: DataDir) -> None:
    (dd.root / "config.toml").mkdir(mode=0o700)
    with pytest.raises(ConfigFileError, match="isn't a regular file"):
        read_config_text(dd)


def test_an_oversized_config_is_refused(dd: DataDir) -> None:
    write(dd, b"#" * (MAX_BYTES + 1))
    with pytest.raises(ConfigFileError, match="larger than"):
        read_config_text(dd)


def test_a_config_that_isnt_utf8_is_refused(dd: DataDir) -> None:
    write(dd, b"\xff\xfe[rpc]")
    with pytest.raises(ConfigFileError, match="UTF-8"):
        read_config_text(dd)
