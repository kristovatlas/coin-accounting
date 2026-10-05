"""Is the data directory on an encrypted volume? (THREAT_MODEL T-401, ADR 0006)

**Linux:** the directory's `st_dev` names its block device; `/sys/dev/block/<major>:<minor>/dm/`
says whether that is a device-mapper device, and which. Accepted, exactly as T-401 lists them: a
`veracrypt*` name, or a `CRYPT-TCRYPT*` / `CRYPT-LUKS*` uuid. Anything else, including a
filesystem with no block device (tmpfs, btrfs subvolumes, network mounts), counts as unencrypted:
the check fails closed.

**macOS:** VeraCrypt mounts through macFUSE or FUSE-T, and no detection method has been verified
yet. T-401's fallback applies: the user confirms the path explicitly, and the confirmation is
stored inside the data directory, tied to its real path and device. A moved or copied directory
needs a new confirmation.

The same module answers "which filesystem type is this?" on Linux, for the FAT/exFAT warning.
"""

from __future__ import annotations

import enum
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SYS_BLOCK: Final = Path("/sys/dev/block")
MOUNTINFO: Final = Path("/proc/self/mountinfo")
CONFIRMATION_FILE: Final = ".encrypted-volume-confirmation.json"
# Filesystems without Unix permission bits: modes such as 0600 can't be enforced there.
NO_PERMISSION_FS: Final = frozenset({"vfat", "msdos", "exfat", "fat", "ntfs", "ntfs3", "fuseblk"})


class Encryption(enum.Enum):
    VERACRYPT = "a VeraCrypt volume"
    LUKS = "a LUKS volume"
    CONFIRMED = "confirmed by the user as an encrypted volume"
    NONE = "not on a recognised encrypted volume"


@dataclass(frozen=True, slots=True)
class VolumeStatus:
    encryption: Encryption
    detail: str
    fs_type: str | None = None

    @property
    def encrypted(self) -> bool:
        return self.encryption is not Encryption.NONE

    @property
    def lacks_permission_bits(self) -> bool:
        return self.fs_type in NO_PERMISSION_FS


def classify_dm(major: int, minor: int, sys_block: Path = SYS_BLOCK) -> tuple[Encryption, str]:
    """Linux: classify the block device `major:minor` from sysfs."""
    dm = sys_block / f"{major}:{minor}" / "dm"
    try:
        name = (dm / "name").read_text().strip()
        uuid = (dm / "uuid").read_text().strip()
    except FileNotFoundError:
        return Encryption.NONE, f"device {major}:{minor} is not a device-mapper device"
    except OSError as e:
        return Encryption.NONE, f"can't read device {major}:{minor} ({e.strerror})"
    if name.startswith("veracrypt") or uuid.startswith("CRYPT-TCRYPT"):
        return Encryption.VERACRYPT, f"device-mapper {name}"
    if uuid.startswith("CRYPT-LUKS"):
        return Encryption.LUKS, f"device-mapper {name}"
    return Encryption.NONE, f"device-mapper {name} isn't a VeraCrypt or LUKS mapping"


def mount_fs_type(path: Path, mountinfo_text: str) -> str | None:
    """Linux: the filesystem type of the mount that contains `path` (the longest mount point that
    is a prefix of it), from /proc/self/mountinfo text."""
    best: tuple[int, str] | None = None
    for line in mountinfo_text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount_point = Path(_unescape_mountinfo(fields[4]))
        if path == mount_point or mount_point in path.parents:
            depth = len(mount_point.parts)
            if best is None or depth >= best[0]:
                best = (depth, right.split()[0])
    return best[1] if best else None


def _unescape_mountinfo(field: str) -> str:
    # The kernel writes space, tab, newline and backslash in mount points as octal escapes.
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(code, char)
    return field


def detect(
    data_dir: Path, *, platform: str = sys.platform, sys_block: Path = SYS_BLOCK, mountinfo: Path = MOUNTINFO
) -> VolumeStatus:
    """Classify the volume `data_dir` lives on. `data_dir` must already be resolved."""
    st = data_dir.stat()
    if platform.startswith("linux"):
        encryption, detail = classify_dm(os.major(st.st_dev), os.minor(st.st_dev), sys_block)
        try:
            fs_type = mount_fs_type(data_dir, mountinfo.read_text())
        except OSError:
            fs_type = None
        return VolumeStatus(encryption, detail, fs_type)
    if platform == "darwin":
        if confirmation_matches(data_dir):
            return VolumeStatus(Encryption.CONFIRMED, f"confirmed in {CONFIRMATION_FILE}")
        return VolumeStatus(Encryption.NONE, "macOS: confirm the encrypted volume once (T-401)")
    return VolumeStatus(Encryption.NONE, f"unsupported platform {platform} (ADR 0003)")


def _identity(data_dir: Path) -> dict[str, object]:
    st = data_dir.stat()
    return {"path": str(data_dir), "device": st.st_dev, "inode": st.st_ino}


def confirmation_matches(data_dir: Path) -> bool:
    """True if `data_dir` holds a confirmation written for this very directory."""
    file = data_dir / CONFIRMATION_FILE
    try:
        st = file.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return False
        recorded = json.loads(file.read_text())
    except (OSError, ValueError):
        return False
    return bool(recorded == _identity(data_dir))


def write_confirmation(data_dir: Path) -> None:
    """Record the user's explicit confirmation (macOS, T-401). Called only from the launcher's
    `--confirm-encrypted-volume` path, after the user has been told what it means."""
    file = data_dir / CONFIRMATION_FILE
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(_identity(data_dir), f)
