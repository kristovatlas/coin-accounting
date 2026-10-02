"""The data directory checks and the unencrypted-storage policy (architecture §6;
THREAT_MODEL T-401, T-406)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coinacct.storage.datadir import DataDirError, clear_tmp, open_data_dir, storage_refusal
from coinacct.storage.volume import Encryption, VolumeStatus


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return d


def test_a_private_directory_is_accepted_and_gets_private_subdirectories(data: Path) -> None:
    dd = open_data_dir(str(data))
    assert dd.root == data.resolve()
    for name in ("tmp", "logs", "exports"):
        assert (data / name).is_dir()
        assert (data / name).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("raw", [None, ""])
def test_there_is_no_default_data_directory_t401(raw: str | None) -> None:
    with pytest.raises(DataDirError, match="no data directory"):
        open_data_dir(raw)


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DataDirError, match="doesn't exist"):
        open_data_dir(str(tmp_path / "nope"))


def test_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with pytest.raises(DataDirError, match="isn't a directory"):
        open_data_dir(str(tmp_path / "f"))


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755, 0o777])
def test_group_or_other_access_is_refused(data: Path, mode: int) -> None:
    data.chmod(mode)
    with pytest.raises(DataDirError, match="chmod 700"):
        open_data_dir(str(data))


@pytest.mark.parametrize("marker", ["dir", "file"])
def test_a_directory_inside_a_git_working_tree_is_refused_t406(tmp_path: Path, marker: str) -> None:
    repo = tmp_path / "repo"
    if marker == "dir":
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    else:  # a linked worktree or submodule
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    inside = repo / "nested" / "data"
    inside.mkdir(parents=True, mode=0o700)
    with pytest.raises(DataDirError, match="inside a git working tree"):
        open_data_dir(str(inside))


def test_a_stray_dot_git_that_git_wouldnt_use_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "parent" / ".git").mkdir(parents=True)  # empty: git doesn't treat this as a repository
    data = tmp_path / "parent" / "data"
    data.mkdir(mode=0o700)
    assert open_data_dir(str(data)).root == data.resolve()


def test_a_symlink_to_the_data_directory_resolves_to_the_real_one(data: Path, tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to(data)
    assert open_data_dir(str(tmp_path / "link")).root == data.resolve()


@pytest.mark.parametrize("bad", ["link", "file", "open"])
def test_unsafe_subdirectories_are_refused(data: Path, tmp_path: Path, bad: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if bad == "link":
        (data / "logs").symlink_to(outside)
    elif bad == "file":
        (data / "logs").write_text("x")
    else:
        (data / "logs").mkdir(mode=0o755)
        (data / "logs").chmod(0o755)
    with pytest.raises(DataDirError):
        open_data_dir(str(data))


def test_paths_outside_the_data_directory_are_refused(data: Path, tmp_path: Path) -> None:
    dd = open_data_dir(str(data))
    assert dd.path("exports", "8949.csv") == data.resolve() / "exports" / "8949.csv"
    with pytest.raises(DataDirError, match="outside the data directory"):
        dd.path("..", "escape.txt")
    (data / "exports" / "sneaky").symlink_to(tmp_path)
    with pytest.raises(DataDirError, match="outside the data directory"):
        dd.path("exports", "sneaky", "x")


def test_clearing_tmp_removes_links_without_following_them(data: Path, tmp_path: Path) -> None:
    dd = open_data_dir(str(data))
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "precious").write_text("x")
    (dd.tmp / "upload.part").write_text("x")
    (dd.tmp / "dir").mkdir()
    (dd.tmp / "dir" / "f").write_text("x")
    (dd.tmp / "link").symlink_to(outside)
    clear_tmp(dd)
    assert list(dd.tmp.iterdir()) == []
    assert (outside / "precious").read_text() == "x"


ENCRYPTED = VolumeStatus(Encryption.VERACRYPT, "device-mapper veracrypt1")
PLAIN = VolumeStatus(Encryption.NONE, "device 8:1 is not a device-mapper device")


@pytest.mark.parametrize(
    ("volume", "chain", "allow", "refused"),
    [
        (ENCRYPTED, "main", False, False),
        (ENCRYPTED, None, False, False),
        (PLAIN, "regtest", False, True),
        (PLAIN, "main", False, True),
        (PLAIN, "regtest", True, False),
        (PLAIN, "signet", True, False),
        (PLAIN, "test", True, False),
        (PLAIN, "testnet4", True, False),
        (PLAIN, "main", True, True),
        (PLAIN, None, True, True),
        (PLAIN, "liquidv1", True, True),
    ],
)
def test_unencrypted_storage_is_allowed_only_off_mainnet_with_the_flag_t401(
    volume: VolumeStatus, chain: str | None, allow: bool, refused: bool
) -> None:
    assert (storage_refusal(volume, chain, allow) is not None) is refused


def test_the_mainnet_refusal_explains_the_flag_t401() -> None:
    message = storage_refusal(PLAIN, "main", True)
    assert message is not None and "never accepted on mainnet" in message
