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

    UV_MODULE = b"# stand-in for uv's _virtualenv.py\n"

    def uv_hook(self):
        """A complete uv hook: the pinned .pth plus a module whose hash the check expects."""
        import hashlib
        from unittest import mock
        (self.site / "_virtualenv.pth").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        (self.site / "_virtualenv.py").write_bytes(self.UV_MODULE)
        patcher = mock.patch.dict(check_pth.UV_HOOK_SHA256,
                                  {"_virtualenv.py": hashlib.sha256(self.UV_MODULE).hexdigest()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return check_pth.main([str(self.venv)])

    def test_uvs_own_file_passes(self):
        self.uv_hook()
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
        self.uv_hook()
        dist = self.site / "fine-1.0.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text("fine/__init__.py,sha256=x,1\nfine-1.0.dist-info/RECORD,,\n")
        self.assertEqual(self.run_main(), 0)

    def test_the_hook_without_its_module_fails(self):
        # PR #74 round 2: without uv's module, `import _virtualenv` loads something else.
        (self.site / "_virtualenv.pth").write_bytes(check_pth.UV_HOOK["_virtualenv.pth"])
        self.assertEqual(self.run_main(), 1)

    def test_anything_that_could_shadow_the_module_fails(self):
        # PR #74 round 2: a package, an extension module or any other _virtualenv.* variant is
        # found before (or instead of) uv's _virtualenv.py.
        for name, make_dir in (("_virtualenv", True), ("_virtualenv.abi3.so", False),
                               ("_virtualenv.cpython-313-x86_64-linux-gnu.so", False), ("_virtualenv.pyc", False)):
            with self.subTest(entry=name):
                self.uv_hook()
                target = self.site / name
                if make_dir:
                    target.mkdir()
                    (target / "__init__.py").write_text("import os\n")
                else:
                    target.write_bytes(b"x")
                self.assertIn("could shadow", " ".join(check_pth.problems(self.venv)))
                if make_dir:
                    (target / "__init__.py").unlink()
                    target.rmdir()
                else:
                    target.unlink()

    def pyc(self, flags: int, mtime: int, size: int) -> None:
        cache = self.site / "__pycache__"
        cache.mkdir(exist_ok=True)
        header = (b"\x00" * 4 + flags.to_bytes(4, "little") + (mtime & 0xFFFFFFFF).to_bytes(4, "little")
                  + (size & 0xFFFFFFFF).to_bytes(4, "little"))
        (cache / "_virtualenv.cpython-313.pyc").write_bytes(header + b"code")

    def test_bytecode_checked_against_uvs_module_passes(self):
        # What the venv's own Python writes on first start (seen in the real .venv).
        self.uv_hook()
        st = (self.site / "_virtualenv.py").stat()
        self.pyc(0, int(st.st_mtime), st.st_size)
        self.assertEqual(check_pth.problems(self.venv), [])

    def test_unchecked_or_stale_bytecode_fails(self):
        self.uv_hook()
        st = (self.site / "_virtualenv.py").stat()
        for flags, mtime, size in ((0b01, int(st.st_mtime), st.st_size),  # unchecked hash-based (PEP 552)
                                   (0b11, int(st.st_mtime), st.st_size),  # checked hash-based
                                   (0, int(st.st_mtime) + 1, st.st_size),  # stale
                                   (0, int(st.st_mtime), st.st_size + 1)):
            with self.subTest(flags=flags, mtime=mtime, size=size):
                self.pyc(flags, mtime, size)
                self.assertIn("isn't bytecode checked against", " ".join(check_pth.problems(self.venv)))

    def test_record_entries_for_a_shadowing_package_or_bytecode_fail(self):
        self.uv_hook()
        dist = self.site / "evil-1.0.dist-info"
        dist.mkdir()
        for path in ("_virtualenv/__init__.py", "./_virtualenv.abi3.so", "__pycache__/_virtualenv.cpython-313.pyc"):
            with self.subTest(record=path):
                (dist / "RECORD").write_text(f"evil/__init__.py,sha256=x,1\n{path},sha256=y,1\n", encoding="utf-8")
                self.assertIn("ships a start-up hook", " ".join(check_pth.problems(self.venv)))

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
