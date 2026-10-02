"""Encrypted-volume detection (THREAT_MODEL T-401, ADR 0006).

Linux detection reads sysfs; the tests build a fake /sys/dev/block for the test directory's real
device number, so every outcome is exercised on any machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coinacct.storage.volume import (
    CONFIRMATION_FILE,
    Encryption,
    classify_dm,
    detect,
    mount_fs_type,
    write_confirmation,
)


def fake_dm(sys_block: Path, major: int, minor: int, name: str, uuid: str) -> None:
    dm = sys_block / f"{major}:{minor}" / "dm"
    dm.mkdir(parents=True)
    (dm / "name").write_text(name + "\n")
    (dm / "uuid").write_text(uuid + "\n")


@pytest.mark.parametrize(
    ("name", "uuid", "expected"),
    [
        ("veracrypt1", "", Encryption.VERACRYPT),
        ("veracrypt12", "CRYPT-TCRYPT-veracrypt12", Encryption.VERACRYPT),
        ("tc-volume", "CRYPT-TCRYPT-tc-volume", Encryption.VERACRYPT),
        ("luks-3f2a", "CRYPT-LUKS2-3f2a8c-luks-3f2a", Encryption.LUKS),
        ("cryptdata", "CRYPT-LUKS1-aa11-cryptdata", Encryption.LUKS),
        ("vg0-home", "LVM-abcdef", Encryption.NONE),
        ("cryptswap", "CRYPT-PLAIN-cryptswap", Encryption.NONE),
        ("myveracrypt", "LVM-x", Encryption.NONE),
    ],
)
def test_device_mapper_devices_are_classified_as_t401_lists_them(
    tmp_path: Path, name: str, uuid: str, expected: Encryption
) -> None:
    fake_dm(tmp_path, 253, 3, name, uuid)
    assert classify_dm(253, 3, tmp_path)[0] is expected


def test_a_device_without_device_mapper_is_unencrypted_t401(tmp_path: Path) -> None:
    (tmp_path / "8:1").mkdir()  # a plain partition: no dm/ directory
    encryption, detail = classify_dm(8, 1, tmp_path)
    assert encryption is Encryption.NONE
    assert "not a device-mapper device" in detail


def test_a_filesystem_with_no_block_device_is_unencrypted_t401(tmp_path: Path) -> None:
    # tmpfs and btrfs subvolumes report a major-0 device with no sysfs entry.
    assert classify_dm(0, 34, tmp_path)[0] is Encryption.NONE


def test_detect_on_linux_uses_the_directorys_own_device(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    st = data.stat()
    sys_block = tmp_path / "sys"
    fake_dm(sys_block, os.major(st.st_dev), os.minor(st.st_dev), "veracrypt3", "CRYPT-TCRYPT-veracrypt3")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"36 1 253:3 / {data} rw,relatime shared:1 - ext4 /dev/mapper/veracrypt3 rw\n")
    status = detect(data, platform="linux", sys_block=sys_block, mountinfo=mountinfo)
    assert (status.encryption, status.fs_type, status.encrypted) == (Encryption.VERACRYPT, "ext4", True)


def test_detect_on_linux_without_a_matching_device_fails_closed_t401(tmp_path: Path) -> None:
    status = detect(
        tmp_path, platform="linux", sys_block=tmp_path / "empty-sys", mountinfo=tmp_path / "missing"
    )
    assert status.encryption is Encryption.NONE
    assert status.fs_type is None
    assert not status.encrypted


MOUNTINFO = r"""22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
40 22 0:34 / /tmp rw,nosuid shared:20 - tmpfs tmpfs rw
51 22 253:3 / /media/veracrypt1 rw,relatime shared:30 - ext4 /dev/mapper/veracrypt1 rw
52 51 8:17 / /media/veracrypt1/usb\040stick rw,relatime shared:31 - vfat /dev/sdb1 rw
53 22 8:33 / /mnt/ex rw,relatime - exfat /dev/sdc1 rw
"""


@pytest.mark.parametrize(
    ("path", "fs"),
    [
        ("/media/veracrypt1/coinacct", "ext4"),
        ("/media/veracrypt1", "ext4"),
        ("/media/veracrypt1/usb stick/x", "vfat"),
        ("/tmp/x", "tmpfs"),
        ("/home/u/data", "ext4"),
        ("/mnt/ex/data", "exfat"),
        ("/media/veracrypt10/x", "ext4"),
    ],
)
def test_the_innermost_mount_decides_the_filesystem_type(path: str, fs: str) -> None:
    assert mount_fs_type(Path(path), MOUNTINFO) == fs


@pytest.mark.parametrize(("fs", "lacks"), [("vfat", True), ("exfat", True), ("ntfs3", True), ("ext4", False)])
def test_fat_and_exfat_are_flagged_for_the_permission_warning_t401(
    tmp_path: Path, fs: str, lacks: bool
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"1 1 0:1 / / rw - {fs} /dev/x rw\n")
    status = detect(tmp_path, platform="linux", sys_block=tmp_path / "sys", mountinfo=mountinfo)
    assert status.lacks_permission_bits is lacks


def test_macos_needs_an_explicit_confirmation_t401(tmp_path: Path) -> None:
    assert detect(tmp_path, platform="darwin").encryption is Encryption.NONE
    write_confirmation(tmp_path)
    assert detect(tmp_path, platform="darwin").encryption is Encryption.CONFIRMED
    assert (tmp_path / CONFIRMATION_FILE).stat().st_mode & 0o777 == 0o600


def test_a_confirmation_copied_to_another_directory_doesnt_count_t401(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_confirmation(first)
    (second / CONFIRMATION_FILE).write_bytes((first / CONFIRMATION_FILE).read_bytes())
    (second / CONFIRMATION_FILE).chmod(0o600)
    assert detect(second, platform="darwin").encryption is Encryption.NONE


def test_a_tampered_or_exposed_confirmation_doesnt_count_t401(tmp_path: Path) -> None:
    write_confirmation(tmp_path)
    file = tmp_path / CONFIRMATION_FILE
    file.chmod(0o644)
    assert detect(tmp_path, platform="darwin").encryption is Encryption.NONE
    file.chmod(0o600)
    record = json.loads(file.read_text())
    record["path"] = "/Volumes/Other"
    file.write_text(json.dumps(record))
    assert detect(tmp_path, platform="darwin").encryption is Encryption.NONE


def test_a_symlinked_confirmation_doesnt_count_t401(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    write_confirmation(real)
    data = tmp_path / "data"
    data.mkdir()
    (data / CONFIRMATION_FILE).symlink_to(real / CONFIRMATION_FILE)
    assert detect(data, platform="darwin").encryption is Encryption.NONE


def test_an_unsupported_platform_is_unencrypted(tmp_path: Path) -> None:
    status = detect(tmp_path, platform="win32")
    assert status.encryption is Encryption.NONE
    assert "unsupported platform" in status.detail
