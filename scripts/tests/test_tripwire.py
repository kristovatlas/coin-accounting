from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import tripwire  # noqa: E402

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class TripwireTests(unittest.TestCase):
    """The tripwire must flag risky changes and stay quiet on plain application code (ADR 0020)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.write("backend/coinacct/tax/engine.py", "def total(a: int, b: int) -> int:\n    return a + b\n")
        self.write("scripts/guard.py", "print('guard')\n")
        self.commit()
        self.base = self.rev()
        self.git("checkout", "-q", "-b", "feature")
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, env=ENV, check=True, capture_output=True,
                              text=True).stdout

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")

    def rev(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def details(self) -> list[tuple[str, str]]:
        return [(f["file"], f["detail"]) for f in tripwire.scan(self.base, self.rev())]

    def test_plain_application_code_raises_no_flags(self):
        self.write("backend/coinacct/tax/engine.py", "def total(a: int, b: int) -> int:\n    return b + a\n")
        self.write("backend/tests/unit/tax/test_engine.py", "def test_total():\n    assert 1 + 1 == 2\n")
        self.commit()
        self.assertEqual(self.details(), [])

    def test_malicious_looking_test_is_flagged(self):
        self.write("backend/tests/unit/tax/test_engine.py",
                   "import subprocess\nimport urllib.request\n"
                   "def test_x():\n    urllib.request.urlopen('https://evil.example/x')\n")
        self.commit()
        found = " ".join(d for _, d in self.details())
        for needle in ("process execution", "network use", "URL host evil.example"):
            with self.subTest(needle=needle):
                self.assertIn(needle, found)

    def test_risky_paths_are_flagged(self):
        for rel in ("backend/tests/conftest.py", "package.json", ".claude/settings.json", "AGENTS.md",
                    "backend/coinacct/api/auth.py", "docs/adr/0099-x.md", "frontend/src/.hidden/x.ts"):
            self.write(rel, "x = 1\n")
        self.commit()
        flagged = {f for f, _ in self.details()}
        for rel in ("backend/tests/conftest.py", "package.json", ".claude/settings.json", "AGENTS.md",
                    "backend/coinacct/api/auth.py", "docs/adr/0099-x.md", "frontend/src/.hidden/x.ts"):
            with self.subTest(path=rel):
                self.assertIn(rel, flagged)

    def test_moving_a_control_file_flags_its_old_path(self):
        self.git("mv", "scripts/guard.py", "backend/coinacct/tax/guard.py")
        self.commit()
        self.assertIn(("scripts/guard.py", "scripts, Makefile or controls"), self.details())

    def test_symlink_blob_and_dynamic_code_are_flagged(self):
        os.symlink("/etc/passwd", self.repo / "backend/coinacct/tax/link")
        self.write("backend/coinacct/tax/rates.py", "DATA = '" + "QUJD" * 40 + "'\nexec(DATA)\n")
        self.commit()
        found = " ".join(d for _, d in self.details())
        for needle in ("symbolic link", "long encoded blob", "dynamic code"):
            with self.subTest(needle=needle):
                self.assertIn(needle, found)

    def test_cli_prints_no_flags(self):
        self.assertEqual(tripwire.render([]), "tripwire: no flags")


if __name__ == "__main__":
    unittest.main()
