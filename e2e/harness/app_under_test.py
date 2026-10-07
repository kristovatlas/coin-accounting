"""The app under test for the E2E specs (ENGINEERING §3.1): a regtest node set up as the app requires,
and the real launcher against it, exactly as a user starts it but with `--no-browser`.

Run as `python -m harness.app_under_test <workdir>` (with `e2e/` and `backend/` on PYTHONPATH). It
prints one JSON line, `{"launch_file": "...", "port": N}`, once the app serves, then waits until
the launcher exits (the spec quits it through the UI) and exits with the launcher's status. On
SIGTERM it stops the launcher and the node. Synthetic regtest data only.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType

from harness.regtest import regtest_node

BACKEND = Path(__file__).resolve().parents[2] / "backend"
ANNOUNCE = re.compile(r"coinacct: launch file (.+)\n")
URL = re.compile(r"http://127\.0\.0\.1:(\d+)/#bootstrap=")


def announce(info: dict[str, object]) -> None:
    """The one line the spec reads (stdout is a pipe, so flush it)."""
    sys.stdout.write(json.dumps(info) + "\n")
    sys.stdout.flush()


def main(workdir: Path) -> int:
    workdir.mkdir(parents=True, exist_ok=True)
    data = workdir / "data"
    data.mkdir(mode=0o700)
    with regtest_node(workdir / "regtest") as node:
        node.mine(101)
        config = data / "config.toml"
        config.write_text(
            f'[rpc]\nhost = "127.0.0.1"\nport = {node.port}\n'
            f'user = "{node.app_user}"\npassword = "{node.app_password}"\n'
        )
        config.chmod(0o600)
        launcher = subprocess.Popen(  # noqa: S603 - fixed argv; this interpreter
            [
                sys.executable,
                "-m",
                "coinacct.launcher",
                "--data-dir",
                str(data),
                "--allow-unencrypted-storage",
                "--no-browser",
            ],
            stdout=subprocess.PIPE,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(workdir), "PYTHONPATH": str(BACKEND)},
        )

        def stop(signum: int, frame: FrameType | None) -> None:
            launcher.terminate()

        signal.signal(signal.SIGTERM, stop)
        assert launcher.stdout is not None
        match = ANNOUNCE.fullmatch(launcher.stdout.readline())
        if match is None:
            launcher.kill()
            announce({"error": "the launcher announced no launch file"})
            return 2
        launch_file = Path(match.group(1))
        port = URL.search(launch_file.read_text())
        announce({"launch_file": str(launch_file), "port": int(port.group(1)) if port else None})
        return launcher.wait()


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
