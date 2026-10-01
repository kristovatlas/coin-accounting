from __future__ import annotations

import hashlib
import io
import os
import subprocess
import shutil
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
        # Isolated from the developer's git config (commit signing, hooks paths, …).
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True,
                              env=env).stdout

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
            for key in ("linux-x86_64", "linux-arm64", "darwin-arm64", "darwin-x86_64"):
                with self.subTest(tool=name, platform=key):
                    entry = lock[name][key]
                    self.assertTrue(entry["url"].startswith("https://"))
                    if "sha256" in entry:
                        self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                    else:
                        self.assertRegex(entry["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")

    def test_pinned_urls_match_their_platform_and_differ(self):
        # #67: a copy-paste slip between platforms would otherwise pass.
        hints = {"linux-x86_64": ("linux",), "linux-arm64": ("linux",), "darwin-arm64": ("darwin", "apple", "macos"),
                 "darwin-x86_64": ("darwin", "apple", "macos")}
        arch = {"linux-x86_64": ("x86_64", "x64", "amd64"), "linux-arm64": ("arm64", "aarch64"),
                "darwin-arm64": ("arm64", "aarch64"), "darwin-x86_64": ("x86_64", "x64", "amd64")}
        for name, spec in toolchain.load_lock().items():
            if name.startswith("_"):
                continue
            urls = [spec[k]["url"] for k in hints]
            with self.subTest(tool=name):
                self.assertEqual(len(set(urls)), len(urls))
            for key in hints:
                url = spec[key]["url"].lower()
                with self.subTest(tool=name, platform=key):
                    self.assertTrue(any(h in url for h in hints[key]), url)
                    self.assertTrue(any(a in url for a in arch[key]), url)

    def test_pnpm_is_the_native_binary_from_the_npm_registry_pinned_by_integrity(self):
        # M0.2: the `pnpm` package is only a launcher that fetches (or downloads at run time) this
        # binary, so the pin is the per-platform @pnpm/exe.<platform> tarball itself.
        for key, entry in toolchain.load_lock()["pnpm"].items():
            if key in ("version", "source"):
                continue
            with self.subTest(platform=key):
                self.assertRegex(entry["url"], r"^https://registry\.npmjs\.org/@pnpm/exe\.[a-z0-9-]+/-/")
                self.assertEqual(entry["kind"], "tar")
                self.assertEqual(entry["bin"], "package/pnpm")
                self.assertRegex(entry["integrity"], r"^sha512-")

    def test_sha256_mismatch_is_a_hard_error(self):
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x", "sha256": "0" * 64,
                                                  "kind": "binary", "bin": "x"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(b"evil")):
            with self.assertRaisesRegex(toolchain.ToolchainError, "hash mismatch"):
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

    def test_installed_tree_is_read_only_so_running_a_tool_cannot_change_it(self):
        # M0.2: the pinned Python wrote .pyc files into its own tree and broke the digest.
        blob = self.wrapped_tar("python/bin/python3", b"#!/bin/sh\n")
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(blob).hexdigest(), "kind": "tar",
                                                  "bin": "python/bin/python3"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            binary = toolchain.install_tool("python", spec, "linux-x86_64")
            tool_dir = Path(d) / "python" / "1"
            for path in [tool_dir, *tool_dir.rglob("*")]:
                with self.subTest(path=path.relative_to(d).as_posix()):
                    self.assertEqual(path.stat().st_mode & 0o222, 0)
            self.assertTrue(binary.stat().st_mode & 0o100)
            if os.geteuid() != 0:  # root ignores permissions
                with self.assertRaises(PermissionError):
                    (binary.parent / "__pycache__").mkdir()
            # A forced reinstall can still replace the read-only tree.
            again = toolchain.install_tool("python", spec, "linux-x86_64", force=True)
            self.assertEqual(again, binary)

    def test_a_valid_writable_install_is_made_read_only_without_reinstalling(self):
        # PR #64 review: an install made before trees were read-only took the cached path and
        # stayed writable, so the pinned Python could still write .pyc files into it.
        blob = self.wrapped_tar("python/bin/python3", b"#!/bin/sh\n")
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(blob).hexdigest(), "kind": "tar",
                                                  "bin": "python/bin/python3"}}
        downloads = []

        def fake_download(url, dest):
            downloads.append(url)
            dest.write_bytes(blob)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", fake_download):
            toolchain.install_tool("python", spec, "linux-x86_64")
            tool_dir = Path(d) / "python" / "1"
            for path in [tool_dir, *tool_dir.rglob("*")]:  # as an install from before this change
                path.chmod(path.stat().st_mode | 0o200)
            toolchain.install_tool("python", spec, "linux-x86_64")
            self.assertEqual(len(downloads), 1)  # the cached install was kept, not replaced
            for path in [tool_dir, *tool_dir.rglob("*")]:
                with self.subTest(path=path.relative_to(d).as_posix()):
                    self.assertEqual(path.stat().st_mode & 0o222, 0)

    def tool_spec(self):
        blob = self.wrapped_tar("python/bin/python3", b"#!/bin/sh\n")
        return blob, {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                       "sha256": hashlib.sha256(blob).hexdigest(), "kind": "tar",
                                                       "bin": "python/bin/python3"}}

    def test_verify_rejects_a_writable_tree_and_install_rehardens_it(self):
        # #69: read-only is what stops a tool changing itself, so verification checks it.
        blob, spec = self.tool_spec()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            toolchain.link(toolchain.install_tool("python", spec, "linux-x86_64"), "python3")
            lock = {"python": spec}
            toolchain.verify_tool("python", lock, "linux-x86_64")
            lib = Path(d) / "python" / "1" / "python" / "bin"
            lib.chmod(0o755)
            with self.assertRaisesRegex(toolchain.ToolchainError, "is writable"):
                toolchain.verify_tool("python", lock, "linux-x86_64")
            toolchain.install_tool("python", spec, "linux-x86_64")  # cached path re-hardens
            toolchain.verify_tool("python", lock, "linux-x86_64")

    def test_a_read_only_tree_needs_no_chmod(self):
        # #69: chmod fails on files another user owns, even when the mode wouldn't change.
        blob, spec = self.tool_spec()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            toolchain.install_tool("python", spec, "linux-x86_64")
            with mock.patch.object(Path, "chmod", side_effect=PermissionError(1, "Operation not permitted")):
                toolchain.make_read_only(Path(d) / "python" / "1")  # nothing writable: no chmod, no error
                again = toolchain.install_tool("python", spec, "linux-x86_64")
            self.assertTrue(again.is_file())

    def test_a_failed_hardening_is_an_error_and_leaves_no_valid_marker(self):
        blob, spec = self.tool_spec()
        real_chmod = Path.chmod

        def refuse_removing_write_bits(path, mode, *a, **k):
            if not mode & 0o200:
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *a, **k)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)), \
                mock.patch.object(Path, "chmod", refuse_removing_write_bits):
            with self.assertRaisesRegex(toolchain.ToolchainError, "another user"):
                toolchain.install_tool("python", spec, "linux-x86_64")
            self.assertIsNone(toolchain.read_marker(Path(d) / "python" / "1"))

    def test_remove_tree_refuses_a_symlinked_tool_dir_before_any_chmod(self):
        with tempfile.TemporaryDirectory() as d:
            outside = Path(d) / "outside"
            outside.mkdir()
            outside.chmod(0o555)
            link = Path(d) / "tool"
            link.symlink_to(outside)
            try:
                with self.assertRaisesRegex(toolchain.ToolchainError, "not a real directory"):
                    toolchain.remove_tree(link)
                self.assertEqual(outside.stat().st_mode & 0o777, 0o555)
            finally:
                outside.chmod(0o755)

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
            path.chmod(0o755)  # installs are read-only; tampering has to undo that first
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
            path.chmod(0o755)  # installs are read-only; tampering has to undo that first
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(toolchain.ToolchainError, "modified"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")

    def test_extraction_without_tarfile_filters_still_refuses_unsafe_members(self):
        # Older host Pythons (e.g. macOS's 3.9) have no tarfile.data_filter (PR #7 review, round 2).
        def archive_with(name, linkname=None):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo(name)
                if linkname:
                    info.type, info.linkname = tarfile.SYMTYPE, linkname
                    tar.addfile(info)
                else:
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
            return buf.getvalue()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(toolchain, "HAS_TAR_FILTERS", False):
            for name, link in (("../escape", None), ("/abs", None), ("ok/link", "/etc/passwd"), ("ok/l2", "../../x")):
                with self.subTest(name=name):
                    archive = Path(d) / "a.tar.gz"
                    archive.write_bytes(archive_with(name, link))
                    target = Path(d) / f"out-{abs(hash(name))}"
                    target.mkdir()
                    with self.assertRaises(tarfile.TarError):
                        toolchain.safe_extract(archive, target)
            archive = Path(d) / "good.tar.gz"
            archive.write_bytes(archive_with("pkg/bin/tool"))
            target = Path(d) / "good"
            target.mkdir()
            toolchain.safe_extract(archive, target)
            self.assertTrue((target / "pkg" / "bin" / "tool").is_file())

    def test_linux_arm64_is_a_supported_platform(self):
        with mock.patch.object(toolchain.platform, "system", return_value="Linux"), \
                mock.patch.object(toolchain.platform, "machine", return_value="aarch64"):
            self.assertEqual(toolchain.platform_key(), "linux-arm64")

    def integrity_tarball(self, files: dict) -> tuple[bytes, str]:
        """A gzipped tarball and its npm-style sha512 integrity, like the @pnpm/exe.<platform> pins."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        import base64
        return blob, "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()

    def pinned_pnpm(self):
        # The layout of the real @pnpm/exe.linux-x64 12.5.1 tarball: the native binary, its
        # package.json, a licence and notices (#62). No Node, no wrapper, no install scripts.
        blob, integrity = self.integrity_tarball({"package/pnpm": b"\x7fELF native pnpm",
                                                  "package/package.json": b'{"name": "@pnpm/exe.linux-x64"}'})
        lock = {"pnpm": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/p.tgz", "integrity": integrity,
                                                          "kind": "tar", "bin": "package/pnpm"}}}
        return lock, blob

    def test_pnpm_native_tarball_is_checked_by_integrity_with_no_wrapper(self):
        # #67: the shipped configuration (kind tar + sha512 integrity) had no install test.
        lock, blob = self.pinned_pnpm()
        spec = lock["pnpm"]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            path = toolchain.install_tool("pnpm", spec, "linux-x86_64")
            self.assertEqual(path, Path(d) / "pnpm" / "1" / "package" / "pnpm")
            self.assertEqual(path.read_bytes(), b"\x7fELF native pnpm")
            self.assertFalse((Path(d) / "pnpm" / "1" / "run").exists())
            self.assertEqual(toolchain.install_tool("pnpm", spec, "linux-x86_64"), path)
        bad = dict(spec["linux-x86_64"], integrity="sha512-" + "A" * 86 + "==")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            with self.assertRaisesRegex(toolchain.ToolchainError, "hash mismatch"):
                toolchain.install_tool("pnpm", {"version": "1", "linux-x86_64": bad}, "linux-x86_64")

    def test_modified_pnpm_is_detected_and_verified_without_node(self):
        # PR #7 review, round 4: every file is covered, not just the entry point. #67: pnpm is
        # native now, so its verification no longer needs (or checks) a Node install.
        lock, blob = self.pinned_pnpm()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            toolchain.link(toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64"), "pnpm")
            toolchain.verify_tool("pnpm", lock, "linux-x86_64")  # no "node" entry in the lock
            for name in ("pnpm", "package.json"):
                with self.subTest(file=name):
                    target = Path(d) / "pnpm" / "1" / "package" / name
                    target.chmod(0o755)  # installs are read-only; tampering has to undo that first
                    target.write_bytes(b"tampered")
                    with self.assertRaisesRegex(toolchain.ToolchainError, "pnpm was modified"):
                        toolchain.verify_tool("pnpm", lock, "linux-x86_64")
                    toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")  # reinstalls
                    toolchain.verify_tool("pnpm", lock, "linux-x86_64")

    def test_moved_checkout_is_reinstalled_not_reused(self):
        # PR #7 review, round 4: the marker records the toolchain root, so a copied tree is
        # treated as not installed rather than trusted.
        lock, blob = self.pinned_pnpm()
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / "a" / ".toolchain", Path(d) / "b" / ".toolchain"
            with mock.patch.object(toolchain, "TOOLCHAIN", old), mock.patch.object(toolchain, "BIN", old / "bin"), \
                    mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
                old.mkdir(parents=True)
                toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")
            shutil.copytree(old, new, symlinks=True)
            with mock.patch.object(toolchain, "TOOLCHAIN", new), mock.patch.object(toolchain, "BIN", new / "bin"), \
                    mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
                with self.assertRaisesRegex(toolchain.ToolchainError, "not installed"):
                    toolchain.verify_tool("pnpm", lock, "linux-x86_64")
                path = toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")
                self.assertTrue(path.is_relative_to(new))
            for tree in (old, new):  # copytree kept the read-only modes
                toolchain.remove_tree(tree)

    def test_planted_bytecode_is_detected(self):
        # Round 5: Python runs a matching .pyc instead of its source, so __pycache__ is covered.
        blob = self.wrapped_tar("python/bin/python3", b"#!/bin/sh\n")
        lock = {"python": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/py.tgz",
                                                            "sha256": hashlib.sha256(blob).hexdigest(),
                                                            "kind": "tar", "bin": "bin/python3"}}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            toolchain.link(toolchain.install_tool("python", lock["python"], "linux-x86_64"), "python3")
            toolchain.verify_tool("python", lock, "linux-x86_64")
            cache = Path(d) / "python" / "1" / "python" / "lib" / "__pycache__"
            for parent in (cache.parents[1], cache.parents[2]):  # undo the read-only install
                parent.chmod(0o755)
            cache.mkdir(parents=True)
            (cache / "os.cpython-313.pyc").write_bytes(b"planted")
            with self.assertRaisesRegex(toolchain.ToolchainError, "python was modified"):
                toolchain.verify_tool("python", lock, "linux-x86_64")

    def test_install_refuses_pins_that_are_not_on_origin_main(self):
        # Round 6: running `toolchain.py install` directly must not skip the approval gate.
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            lock = root / "scripts" / "toolchain.lock"
            lock.write_text("{}\n")
            git = lambda *a: subprocess.run(["git", "-C", d, *a], env=env, check=True, capture_output=True)
            # Everything the install path touches points into the temporary repo, so a regression
            # can't write into the real checkout's .toolchain (it once did, via a default argument).
            with mock.patch.object(toolchain, "ROOT", root), mock.patch.object(toolchain, "LOCK", lock), \
                    mock.patch.object(toolchain, "TOOLCHAIN", root / ".toolchain"), \
                    mock.patch.object(toolchain, "BIN", root / ".toolchain" / "bin"), \
                    mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
                git("init", "-q")
                with self.assertRaisesRegex(toolchain.ToolchainError, "unknown"):
                    toolchain.require_approved_lock(root)
                git("add", "-A")
                git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
                git("update-ref", "refs/remotes/origin/main", "HEAD")
                toolchain.require_approved_lock(root)
                lock.write_text('{"sfw": {}}\n')
                with self.assertRaisesRegex(toolchain.ToolchainError, "differs from origin/main.*AI agents: stop"):
                    toolchain.require_approved_lock(root)
                with mock.patch.object(toolchain, "install_tool") as install, \
                        mock.patch.object(toolchain, "link") as link:
                    self.assertEqual(toolchain.main(["install"]), 1)
                    install.assert_not_called()
                    link.assert_not_called()
                    # Once the pins are merged, the same call goes ahead.
                    git("add", "-A")
                    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "y")
                    git("update-ref", "refs/remotes/origin/main", "HEAD")
                    toolchain.require_approved_lock()

    def test_verify_without_an_artifact_for_this_platform_is_a_clean_error(self):
        lock = {"sfw": {"version": "1", "darwin-arm64": {}}}
        with self.assertRaisesRegex(toolchain.ToolchainError, "no artifact pinned"):
            toolchain.verify_tool("sfw", lock, "linux-x86_64")

    def test_non_https_url_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(toolchain.ToolchainError, "non-HTTPS"):
                toolchain.download("http://example.invalid/x", Path(d) / "x")


class HostPythonCompatibilityTests(unittest.TestCase):
    def test_union_annotations_need_the_future_import(self):
        # Round 5: `str | None` in an evaluated annotation raises TypeError on the host minimum, 3.9.
        import ast
        for path in sorted(Path(__file__).resolve().parent.parent.rglob("*.py")):
            tree = ast.parse(path.read_text(), feature_version=(3, 9))
            future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                         and any(a.name == "annotations" for a in n.names) for n in tree.body)
            annotations = [n.annotation for n in ast.walk(tree) if isinstance(n, (ast.arg, ast.AnnAssign)) and n.annotation]
            annotations += [n.returns for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns]
            uses_union = any(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)
                             for a in annotations for sub in ast.walk(a))
            with self.subTest(file=path.name):
                self.assertTrue(future or not uses_union, "add `from __future__ import annotations`")


if __name__ == "__main__":
    unittest.main()
