"""Read `<data>/config.toml` (architecture §6; THREAT_MODEL T-201).

It holds the RPC credentials, so it must be a regular file owned by this user with no group or
other access. The text goes to `coinacct.config.parse` (the launcher does that: `storage` doesn't
import `config`, architecture §2).
"""

from __future__ import annotations

import os
import stat
from typing import Final

from coinacct.storage.datadir import DataDir

NAME: Final = "config.toml"
MAX_BYTES: Final = 64 * 1024


class ConfigFileError(Exception):
    pass


def read_config_text(data_dir: DataDir) -> str:
    # Not data_dir.path(): that resolves links, and a linked config must be refused, not followed.
    path = data_dir.root / NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConfigFileError(
            f"{path} doesn't exist: create it with the [rpc] settings (see the docs)"
        ) from None
    except OSError as e:
        raise ConfigFileError(
            f"can't open {path} ({e.strerror}); it must be a regular file, not a link"
        ) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigFileError(f"{path} isn't a regular file")
        if st.st_uid != os.getuid():
            raise ConfigFileError(f"{path} is owned by another user")
        if st.st_dev != data_dir.device:
            raise ConfigFileError(
                f"{path} is on a different filesystem from the data directory (a mount?) (T-401)"
            )
        if st.st_mode & 0o077:
            raise ConfigFileError(f"{path} holds the RPC password: run chmod 600 on it (T-201)")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ConfigFileError(f"{path} is larger than {MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigFileError(f"{path} isn't UTF-8 text") from None
