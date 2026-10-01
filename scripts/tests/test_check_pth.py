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
        (self.site / "_virtualenv.pth").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        self.assertEqual(self.run_main(), 0)

    def test_any_other_pth_fails(self):
        (self.site / "evil.pth").write_text("import os\n")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual([p.name for p in check_pth.unexpected(self.venv)], ["evil.pth"])

    def test_uvs_own_file_with_other_content_fails(self):
        # PR #74 review: the allowed name alone proves nothing.
        (self.site / "_virtualenv.pth").write_text("import os; os.system('x')\n")
        self.assertEqual(self.run_main(), 1)

    def test_a_symlinked_hook_fails(self):
        (self.site / "real.txt").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        (self.site / "_virtualenv.pth").symlink_to(self.site / "real.txt")
        self.assertEqual(self.run_main(), 1)

    def test_a_replaced_virtualenv_module_fails(self):
        (self.site / "_virtualenv.pth").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        (self.site / "_virtualenv.py").write_text("import os\n")
        self.assertEqual(self.run_main(), 1)

    def test_a_package_that_ships_a_hook_in_its_record_fails(self):
        (self.site / "_virtualenv.pth").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        dist = self.site / "evil-1.0.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text("evil/__init__.py,sha256=x,1\n_virtualenv.pth,sha256=y,18\n")
        self.assertEqual(self.run_main(), 1)
        self.assertIn("evil-1.0.dist-info: _virtualenv.pth", " ".join(check_pth.problems(self.venv)))

    def test_a_package_with_only_its_own_files_passes(self):
        (self.site / "_virtualenv.pth").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        dist = self.site / "fine-1.0.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text("fine/__init__.py,sha256=x,1\nfine-1.0.dist-info/RECORD,,\n")
        self.assertEqual(self.run_main(), 0)

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
