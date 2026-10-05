"""The verified data directory (architecture §6; THREAT_MODEL T-401, T-402, T-406).

`<data>` comes from `--data-dir` or `COINACCT_DATA_DIR` each time; there is no default. It is
accepted only if it resolves to an existing directory owned by this user with no group or other
access, outside any git working tree. `storage/` then refuses any path that doesn't resolve under
it. Whether it may be used at all depends on the volume (`volume.py`) and the chain:
`--allow-unencrypted-storage` is honoured only off mainnet.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from coinacct.storage.volume import VolumeStatus, detect

SUBDIRS: Final = ("tmp", "logs", "exports")
# Chains where unencrypted storage may be allowed for development and CI (T-401). Anything else,
# including an unknown chain, is treated as mainnet.
TEST_CHAINS: Final = frozenset({"regtest", "signet", "test", "testnet4"})


class DataDirError(Exception):
    """The data directory can't be used. The message says what to fix."""


@dataclass(frozen=True, slots=True)
class DataDir:
    root: Path
    volume: VolumeStatus
    # The device `volume` describes. Anything under `root` on another device (a mount or bind mount
    # at `<data>/exports`, say) wasn't classified, so it is refused rather than trusted (T-401).
    device: int
    # The root's inode when it was verified: with `device`, the identity the watchdog keeps checking
    # (T-405), so a directory swapped in after verification is never trusted.
    inode: int

    def path(self, *parts: str) -> Path:
        """A path under the data directory, on the verified device; refuses anything that resolves
        outside it (§6) or whose nearest existing part is on another filesystem (T-401)."""
        candidate = self.root.joinpath(*parts).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise DataDirError("refusing a path outside the data directory")
        existing = next(p for p in (candidate, *candidate.parents) if os.path.lexists(p))
        require_device(existing, os.lstat(existing), self.device)
        return candidate

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"


def open_data_dir(raw: str | None) -> DataDir:
    """Verify `raw` (from --data-dir or COINACCT_DATA_DIR) and prepare its subdirectories."""
    if not raw:
        raise DataDirError(
            "no data directory: pass --data-dir or set COINACCT_DATA_DIR to a directory on your encrypted "
            "volume. There is no default, so the path is never saved on plain disk (T-401)"
        )
    try:
        root = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise DataDirError(
            "the data directory doesn't exist (create it on the encrypted volume first)"
        ) from None
    st = root.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise DataDirError("the data directory isn't a directory")
    check_private(root, st, "the data directory")
    repo = enclosing_git_worktree(root)
    if repo is not None:
        raise DataDirError(
            f"the data directory is inside a git working tree ({repo}); "
            "move it out so it can't be committed (T-406)"
        )
    for name in SUBDIRS:
        ensure_private_subdir(root / name, st.st_dev)
    return DataDir(root=root, volume=detect(root), device=st.st_dev, inode=st.st_ino)


def check_private(path: Path, st: os.stat_result, what: str) -> None:
    if st.st_uid != os.getuid():
        raise DataDirError(f"{what} ({path}) is owned by another user")
    if st.st_mode & 0o077:
        raise DataDirError(f"{what} ({path}) is readable or writable by others: run chmod 700 on it")


def enclosing_git_worktree(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if is_git_marker(candidate / ".git"):
            return candidate
    return None


def is_git_marker(dot_git: Path) -> bool:
    """What git's own discovery accepts: a `.git` directory with a HEAD file, or a `.git` file
    pointing elsewhere (`gitdir: ...`, used by linked worktrees and submodules)."""
    try:
        if dot_git.is_dir():
            return (dot_git / "HEAD").is_file()
        if dot_git.is_file():
            with dot_git.open("rb") as f:
                return f.read(7) == b"gitdir:"
    except OSError:
        return True  # can't tell: fail closed
    return False


def require_device(path: Path, st: os.stat_result, device: int) -> None:
    if st.st_dev != device:
        raise DataDirError(
            f"{path} is on a different filesystem from the data directory (a mount?); everything must be "
            "on the verified volume (T-401)"
        )


def ensure_private_subdir(path: Path, device: int) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise DataDirError(f"{path} must be a directory, not a link or a file")
    require_device(path, st, device)
    check_private(path, st, path.name)


def clear_tmp(data_dir: DataDir) -> None:
    """Empty `<data>/tmp` (at start and at shutdown, architecture §6). Links are removed, never
    followed, and the walk never crosses into another filesystem: `tmp` itself and every directory
    under it must be a real directory on the verified device, or nothing more is removed."""
    st = data_dir.tmp.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise DataDirError(f"{data_dir.tmp} must be a directory, not a link or a file")
    require_device(data_dir.tmp, st, data_dir.device)
    for entry in os.scandir(data_dir.tmp):
        _remove(Path(entry.path), data_dir.device)


def _remove(path: Path, device: int) -> None:
    st = path.lstat()
    if stat.S_ISDIR(st.st_mode):
        require_device(path, st, device)  # a mount point inside tmp: refuse, never descend
        for entry in os.scandir(path):
            _remove(Path(entry.path), device)
        path.rmdir()
    else:
        path.unlink()


def storage_refusal(volume: VolumeStatus, chain: str | None, allow_unencrypted: bool) -> str | None:
    """Pure policy (T-401): why the data directory may not be used, or None if it may.

    `chain` is the node's or the data directory's chain; None (not known yet) counts as mainnet.
    """
    if volume.encrypted:
        return None
    if not allow_unencrypted:
        return (
            f"the data directory is {volume.encryption.value} ({volume.detail}). Put it on a VeraCrypt "
            "volume; --allow-unencrypted-storage exists only for regtest, signet and testnet (T-401)"
        )
    if chain not in TEST_CHAINS:
        return (
            "--allow-unencrypted-storage is never accepted on mainnet, "
            "or when the chain isn't known yet (T-401)"
        )
    return None
