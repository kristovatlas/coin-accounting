from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import check_pth  # noqa: E402

if sys.version_info >= (3, 11):
    import tomllib
else:  # host Python < 3.11: the pyproject test below is skipped
    tomllib = None


class CheckPthTests(unittest.TestCase):
    """ENGINEERING §2.2 / T-602: a `.pth` file from a wheel runs code on every interpreter start."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.venv = Path(self._tmp.name) / ".venv"
        self.site = self.venv / "lib" / "python3.13" / "site-packages"
        self.site.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return check_pth.main([str(self.venv)])

    def test_uvs_own_file_passes(self):
        (self.site / "_virtualenv.pth").write_text("import _virtualenv\n")
        self.assertEqual(self.run_main(), 0)

    def test_any_other_pth_fails(self):
        (self.site / "evil.pth").write_text("import os\n")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual([p.name for p in check_pth.unexpected(self.venv)], ["evil.pth"])

    def test_a_missing_environment_fails_rather_than_passing(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(check_pth.main([str(Path(self._tmp.name) / "nope")]), 1)


@unittest.skipIf(tomllib is None, "needs Python 3.11+ for tomllib")
class PytestConfigTests(unittest.TestCase):
    """PR #65 review: pytest's built-in pastebin plugin uploads test output to bpa.st on
    --pastebin; it is disabled in pyproject.toml, and this keeps it disabled."""

    def test_pastebin_plugin_is_disabled(self):
        config = tomllib.loads((HERE.parent / "pyproject.toml").read_text())
        addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
        self.assertIn("-p no:pastebin", addopts if isinstance(addopts, str) else " ".join(addopts))


if __name__ == "__main__":
    unittest.main()
