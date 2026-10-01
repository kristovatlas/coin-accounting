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
        self.assertIn("unexpected .pth file", " ".join(p for p in check_pth.problems(self.venv) if "evil.pth" in p))

    def test_uvs_own_file_with_other_content_fails(self):
        # PR #74 review: the allowed name alone proves nothing. Round 3: start from a complete
        # hook, so only the content check can catch this.
        self.uv_hook()
        (self.site / "_virtualenv.pth").write_text("import os; os.system('x')\n")
        self.assertIn("_virtualenv.pth is not uv's own file", " ".join(check_pth.problems(self.venv)))

    def test_a_symlinked_hook_or_module_fails(self):
        for name in ("_virtualenv.pth", "_virtualenv.py"):
            with self.subTest(file=name):
                self.uv_hook()
                real = self.site / f"real-{name}"
                real.write_bytes((self.site / name).read_bytes())
                (self.site / name).unlink()
                (self.site / name).symlink_to(real)
                self.assertIn(f"{name} is not uv's own file", " ".join(check_pth.problems(self.venv)))
                (self.site / name).unlink()
                real.unlink()

    def test_case_variant_names_fail(self):
        # Round 3: with PYTHONCASEOK on a case-insensitive file system, `_VIRTUALENV/` is found too.
        self.uv_hook()
        (self.site / "_VIRTUALENV").mkdir()
        (self.site / "_VIRTUALENV" / "__init__.py").write_text("import os\n")
        self.assertIn("could shadow", " ".join(check_pth.problems(self.venv)))
        dist = self.site / "evil-1.0.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text("_VirtualEnv.abi3.so,sha256=y,1\n", encoding="utf-8")
        self.assertIn("ships a start-up hook", " ".join(check_pth.problems(self.venv)))

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

    def test_any_cached_bytecode_for_the_hook_fails(self):
        # Round 3: a .pyc whose header copies the source's mtime and size runs whatever code it
        # holds, and Python accepts it. `make bootstrap` deletes these caches before the check.
        self.uv_hook()
        st = (self.site / "_virtualenv.py").stat()
        cache = self.site / "__pycache__"
        cache.mkdir()
        header = (b"\x00" * 8 + (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
                  + st.st_size.to_bytes(4, "little"))
        for name in ("_virtualenv.cpython-313.pyc", "_VIRTUALENV.cpython-313.pyc"):
            with self.subTest(pyc=name):
                (cache / name).write_bytes(header + b"forged code")
                self.assertIn("cached bytecode for uv's hook isn't allowed", " ".join(check_pth.problems(self.venv)))
                (cache / name).unlink()
        (cache / "py.cpython-313.pyc").write_bytes(header)  # other modules' caches are fine
        self.assertEqual(check_pth.problems(self.venv), [])

    def test_record_entries_for_a_shadowing_package_or_bytecode_fail(self):
        self.uv_hook()
        dist = self.site / "evil-1.0.dist-info"
        dist.mkdir()
        for path in ("_virtualenv/__init__.py", "./_virtualenv.abi3.so", "__pycache__/_virtualenv.cpython-313.pyc"):
            with self.subTest(record=path):
                (dist / "RECORD").write_text(f"evil/__init__.py,sha256=x,1\n{path},sha256=y,1\n", encoding="utf-8")
                self.assertIn("ships a start-up hook", " ".join(check_pth.problems(self.venv)))

    # coverage 7.16.1's `a1_coverage.pth`, byte for byte (identical in every locked wheel; ADR 0025).
    COVERAGE_PTH = b'import sys; exec(\'import os\\n\\nif os.getenv("COVERAGE_PROCESS_START") or os.getenv("COVERAGE_PROCESS_CONFIG"):\\n try:\\n  import coverage\\n except:\\n  pass\\n else:\\n  coverage.process_startup(slug="pth")\')\n'

    def coverage_hook(self, record_row=None):
        """coverage's hook plus a coverage dist-info whose RECORD claims it with the pinned hash."""
        (self.site / check_pth.COVERAGE_HOOK).write_bytes(self.COVERAGE_PTH)
        dist = self.site / "coverage-7.16.1.dist-info"
        dist.mkdir(exist_ok=True)
        row = record_row or f"a1_coverage.pth,{check_pth.record_hash(check_pth.COVERAGE_HOOK_SHA256)},205"
        (dist / "RECORD").write_text(f"coverage/__init__.py,sha256=x,1\n{row}\n", encoding="utf-8")

    def test_the_pinned_hash_is_coverages_real_file(self):
        import hashlib
        self.assertEqual(hashlib.sha256(self.COVERAGE_PTH).hexdigest(), check_pth.COVERAGE_HOOK_SHA256)
        self.assertEqual(check_pth.record_hash(check_pth.COVERAGE_HOOK_SHA256),
                         "sha256=7y7QbRmGfsZpwJqAQGBmapzV44OvCp0Rqi3nm3fUSOg")  # from coverage's own RECORD

    def test_coverages_own_hook_passes(self):
        self.uv_hook()
        self.coverage_hook()
        self.assertEqual(check_pth.problems(self.venv), [])

    def test_coverages_hook_with_other_content_fails(self):
        self.uv_hook()
        self.coverage_hook()
        (self.site / check_pth.COVERAGE_HOOK).write_bytes(self.COVERAGE_PTH.replace(b"pass", b"import os"))
        self.assertIn("is not coverage's own file", " ".join(check_pth.problems(self.venv)))

    def test_a_symlinked_coverage_hook_fails(self):
        self.uv_hook()
        self.coverage_hook()
        real = self.site / "real.txt"
        real.write_bytes(self.COVERAGE_PTH)
        (self.site / check_pth.COVERAGE_HOOK).unlink()
        (self.site / check_pth.COVERAGE_HOOK).symlink_to(real)
        self.assertIn("is not coverage's own file", " ".join(check_pth.problems(self.venv)))

    def test_coverages_hook_without_coverage_installed_fails(self):
        self.uv_hook()
        (self.site / check_pth.COVERAGE_HOOK).write_bytes(self.COVERAGE_PTH)
        self.assertIn("no installed coverage distribution claims it", " ".join(check_pth.problems(self.venv)))

    def test_coverage_claiming_the_hook_with_another_hash_fails(self):
        self.uv_hook()
        self.coverage_hook(record_row="a1_coverage.pth,sha256=AAAA,205")
        found = " ".join(check_pth.problems(self.venv))
        self.assertIn("ships a start-up hook", found)
        self.assertIn("no installed coverage distribution claims it", found)

    def test_another_package_shipping_coverages_hook_or_module_fails(self):
        # Only coverage may ship the hook, and only coverage may provide the module it imports.
        self.uv_hook()
        self.coverage_hook()
        dist = self.site / "evil-1.0.dist-info"
        dist.mkdir()
        good = check_pth.record_hash(check_pth.COVERAGE_HOOK_SHA256)
        for path in (f"a1_coverage.pth,{good},205", "coverage/__init__.py,sha256=y,1", "Coverage.abi3.so,sha256=y,1"):
            with self.subTest(record=path):
                (dist / "RECORD").write_text(f"evil/__init__.py,sha256=x,1\n{path}\n", encoding="utf-8")
                self.assertIn("evil-1.0.dist-info", " ".join(check_pth.problems(self.venv)))

    def test_case_variant_coverage_hook_name_fails(self):
        self.uv_hook()
        self.coverage_hook()
        (self.site / check_pth.COVERAGE_HOOK).rename(self.site / "A1_Coverage.pth")
        self.assertIn("unexpected .pth file", " ".join(check_pth.problems(self.venv)))

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
