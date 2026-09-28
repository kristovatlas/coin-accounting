import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import check_adrs  # noqa: E402
import toolchain  # noqa: E402

ADR = """---
status: accepted
date: 2026-09-28
deciders: human
{extra}---

# {num}: {title}

Body.
"""


def make_repo(root: Path, arch: str | None, adrs: list[tuple[str, str, str]], index: bool = True) -> None:
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    rows = []
    for num, title, extra in adrs:
        (adr_dir / f"{num}-x.md").write_text(ADR.format(num=num, title=title, extra=extra))
        rows.append(f"| [{num}]({num}-x.md) | {title} |")
    if index:
        (adr_dir / "README.md").write_text("| ADR | Title |\n|---|---|\n" + "\n".join(rows) + "\n")
    if arch is not None:
        (root / "docs" / "architecture.md").write_text(arch)


class CheckAdrsTests(unittest.TestCase):
    def check(self, root: Path) -> list[str]:
        errors, metas, titles = check_adrs.check_files(root / "docs" / "adr")
        return errors + check_adrs.check_index(root / "docs" / "adr", titles) + check_adrs.check_hash(root, metas)

    def test_valid_repo_passes(self):
        with tempfile.TemporaryDirectory() as d:
            h = hashlib.sha256(b"arch").hexdigest()
            make_repo(Path(d), "arch", [("0001", "One", ""), ("0002", "Two", f"architecture_sha256: {h}\n")])
            self.assertEqual(self.check(Path(d)), [])

    def test_hash_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d), "changed", [("0001", "One", "architecture_sha256: " + "0" * 64 + "\n")])
            errors = self.check(Path(d))
            self.assertEqual(len(errors), 1)
            self.assertIn("does not match ADR 0001", errors[0])

    def test_highest_numbered_hash_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            new = hashlib.sha256(b"v2").hexdigest()
            make_repo(Path(d), "v2", [("0001", "A", "architecture_sha256: " + "0" * 64 + "\n"),
                                      ("0005", "B", f"architecture_sha256: {new}\n")])
            self.assertEqual(self.check(Path(d)), [])

    def test_index_must_match_files(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d), None, [("0001", "One", "")], index=False)
            (Path(d) / "docs/adr/README.md").write_text("| [0001](0001-x.md) | Wrong |\n| [0002](0002-x.md) | Ghost |\n")
            errors = self.check(Path(d))
            self.assertTrue(any("title of ADR 0001" in e for e in errors))
            self.assertTrue(any("lists ADR 0002" in e for e in errors))

    def test_invalid_status_fails(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d), None, [("0001", "One", "")])
            p = Path(d) / "docs/adr/0001-x.md"
            p.write_text(p.read_text().replace("status: accepted", "status: maybe"))
            self.assertTrue(any("invalid status" in e for e in self.check(Path(d))))


class AdrBaseComparisonTests(unittest.TestCase):
    """ADR 0001 immutability against the base branch (check_against_base)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = hashlib.sha256(b"arch").hexdigest()
        make_repo(self.root, "arch", [("0001", "One", ""), ("0002", "Two", f"architecture_sha256: {self.h}\n")])
        self.git("init", "-q")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def check(self):
        errors, metas, _ = check_adrs.check_files(self.root / "docs" / "adr")
        errors += check_adrs.duplicate_numbers(self.root / "docs" / "adr")
        return errors + check_adrs.check_against_base(self.root, self.base, metas)

    def adr(self, num):
        return self.root / "docs" / "adr" / f"{num}-x.md"

    def test_unchanged_passes(self):
        self.assertEqual(self.check(), [])

    def test_body_change_fails(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("Body.", "Rewritten."))
        self.assertTrue(any("may only change its status line" in e for e in self.check()))

    def test_deleted_adr_fails(self):
        self.adr("0001").unlink()
        self.assertTrue(any("was removed" in e for e in self.check()))

    def test_renamed_adr_fails(self):
        self.adr("0001").rename(self.root / "docs" / "adr" / "0001-renamed.md")
        self.assertTrue(any("was renamed" in e for e in self.check()))

    def test_duplicate_number_fails(self):
        (self.root / "docs" / "adr" / "0001-copy.md").write_text(self.adr("0001").read_text())
        self.assertTrue(any("used by several files" in e for e in self.check()))

    def test_status_back_to_proposed_fails(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("status: accepted", "status: proposed"))
        self.assertTrue(any("may not change from 'accepted' to 'proposed'" in e for e in self.check()))

    def test_deprecated_is_allowed(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("status: accepted", "status: deprecated"))
        self.assertEqual(self.check(), [])

    def test_superseded_needs_a_successor_pointing_back(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("status: accepted", "status: superseded by 0003"))
        self.assertTrue(any("may not change" in e for e in self.check()))
        (self.root / "docs" / "adr" / "0003-x.md").write_text(ADR.format(num="0003", title="Three", extra="supersedes: 0001\n"))
        readme = self.root / "docs" / "adr" / "README.md"
        readme.write_text(readme.read_text() + "| [0003](0003-x.md) | Three |\n")
        self.assertEqual(self.check(), [])

    def test_architecture_change_needs_one_new_hashed_adr(self):
        (self.root / "docs" / "architecture.md").write_text("arch v2")
        self.assertTrue(any("add exactly one new ADR" in e for e in self.check()))
        h2 = hashlib.sha256(b"arch v2").hexdigest()
        (self.root / "docs" / "adr" / "0003-x.md").write_text(
            ADR.format(num="0003", title="Three", extra=f"architecture_sha256: {h2}\n"))
        self.assertEqual([e for e in self.check() if "exactly one new ADR" in e], [])


class ToolchainTests(unittest.TestCase):
    def test_lock_has_every_tool_for_every_platform(self):
        lock = toolchain.load_lock()
        for name in ("sfw", "pnpm", "uv", "node", "python", "bitcoind"):
            for key in ("linux-x86_64", "darwin-arm64", "darwin-x86_64"):
                with self.subTest(tool=name, platform=key):
                    entry = lock[name][key]
                    self.assertTrue(entry["url"].startswith("https://"))
                    self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

    def test_sha256_mismatch_is_a_hard_error(self):
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x", "sha256": "0" * 64,
                                                  "kind": "binary", "bin": "x"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(b"evil")):
            with self.assertRaisesRegex(toolchain.ToolchainError, "SHA-256 mismatch"):
                toolchain.install_tool("x", spec, "linux-x86_64")
            self.assertFalse((Path(d) / "x").exists())

    def test_matching_binary_is_installed_executable(self):
        data = b"#!/bin/sh\necho ok\n"
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(data).hexdigest(),
                                                  "kind": "binary", "bin": "tool"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(data)):
            path = toolchain.install_tool("x", spec, "linux-x86_64")
            self.assertEqual(path.read_bytes(), data)
            self.assertTrue(path.stat().st_mode & 0o100)

    def test_tar_with_path_traversal_is_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "a.tar.gz"
            archive.write_bytes(buf.getvalue())
            target = Path(d) / "out"
            target.mkdir()
            with self.assertRaises(tarfile.TarError):
                toolchain.safe_extract(archive, target)

    def wrapped_tar(self, inner: str, data: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(inner)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_second_install_of_wrapped_tarball_returns_the_real_binary(self):
        # PR #7 review: the cache hit used to return tool_dir/entry["bin"], which doesn't exist for
        # tarballs that wrap their content in a top-level directory (uv, Node).
        blob = self.wrapped_tar("uv-x86_64-unknown-linux-gnu/uv", b"#!/bin/sh\n")
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(blob).hexdigest(), "kind": "tar", "bin": "uv"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            first = toolchain.install_tool("uv", spec, "linux-x86_64")
            second = toolchain.install_tool("uv", spec, "linux-x86_64")
            self.assertEqual(first, second)
            self.assertTrue(second.is_file())

    def test_modified_binary_is_reinstalled_on_the_next_run(self):
        data = b"#!/bin/sh\n"
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(data).hexdigest(), "kind": "binary", "bin": "sfw"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(data)):
            path = toolchain.install_tool("sfw", spec, "linux-x86_64")
            path.write_bytes(b"tampered")
            again = toolchain.install_tool("sfw", spec, "linux-x86_64")
            self.assertEqual(again.read_bytes(), data)

    def test_verify_accepts_the_pinned_binary_and_rejects_substitutes(self):
        data = b"#!/bin/sh\n"
        lock = {"sfw": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                         "sha256": hashlib.sha256(data).hexdigest(),
                                                         "kind": "binary", "bin": "sfw"}}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(data)):
            with self.assertRaisesRegex(toolchain.ToolchainError, "not installed"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")
            path = toolchain.install_tool("sfw", lock["sfw"], "linux-x86_64")
            toolchain.link(path, "sfw")
            self.assertEqual(toolchain.verify_tool("sfw", lock, "linux-x86_64"), path.resolve())
            # A symlink pointing somewhere else (e.g. /usr/bin/env) is refused.
            (Path(d) / "bin" / "sfw").unlink()
            (Path(d) / "bin" / "sfw").symlink_to("/usr/bin/env")
            with self.assertRaisesRegex(toolchain.ToolchainError, "does not point"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")
            toolchain.link(path, "sfw")
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(toolchain.ToolchainError, "modified"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")

    def test_non_https_url_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(toolchain.ToolchainError, "non-HTTPS"):
                toolchain.download("http://example.invalid/x", Path(d) / "x")


if __name__ == "__main__":
    unittest.main()
