from __future__ import annotations

import argparse
import hashlib
import io
import os
import subprocess
import shutil
import sys
import tarfile
import tempfile
import unittest
import urllib.request
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
        for name in ("sfw", "pnpm", "uv", "node", "python", "bitcoind", "osv-scanner"):
            for key in ("linux-x86_64", "linux-arm64", "darwin-arm64", "darwin-x86_64"):
                with self.subTest(tool=name, platform=key):
                    entry = lock[name][key]
                    self.assertTrue(entry["url"].startswith("https://"))
                    if "sha256" in entry:
                        self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                    else:
                        self.assertRegex(entry["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")

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

    def test_a_symlink_above_the_tool_dir_is_refused_everywhere(self):
        # PR #83 review: a symlinked .toolchain/<name> (or .toolchain) was followed by
        # chmod/rmtree/os.walk, which then acted on files outside .toolchain.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root / "outside"
            (outside / "1" / "sub").mkdir(parents=True)
            (outside / "1" / "sub" / "f").write_text("x")
            for path in (outside / "1" / "sub" / "f", outside / "1" / "sub", outside / "1"):
                path.chmod(0o555 if path.is_dir() else 0o444)
            tc = root / ".toolchain"
            tc.mkdir()
            (tc / "python").symlink_to(outside)
            blob, spec = self.tool_spec()
            try:
                with mock.patch.object(toolchain, "TOOLCHAIN", tc):
                    for call in (lambda: toolchain.remove_tree(tc / "python" / "1"),
                                 lambda: toolchain.make_read_only(tc / "python" / "1"),
                                 lambda: toolchain.writable_paths(tc / "python" / "1"),
                                 lambda: toolchain.tree_digest(tc / "python" / "1"),
                                 lambda: toolchain.install_tool("python", spec, "linux-x86_64")):
                        with self.subTest(call=call):
                            with self.assertRaisesRegex(toolchain.ToolchainError, "not a real directory"):
                                call()
                self.assertEqual((outside / "1").stat().st_mode & 0o777, 0o555)
                self.assertTrue((outside / "1" / "sub" / "f").exists())
                linked = root / "linked"
                linked.symlink_to(tc)
                with mock.patch.object(toolchain, "TOOLCHAIN", linked):
                    with self.assertRaisesRegex(toolchain.ToolchainError, "not a real directory"):
                        toolchain.check_tool_path(linked / "python" / "1")
            finally:
                for path in (outside / "1", outside / "1" / "sub"):
                    path.chmod(0o755)

    @unittest.skipIf(os.geteuid() == 0, "root can list any directory")
    def test_an_unreadable_directory_fails_closed(self):
        # PR #83 review: os.walk skipped directories it couldn't list, so a writable file under
        # one was missed by the write-bit check and the digest.
        with tempfile.TemporaryDirectory() as d:
            tool_dir = Path(d) / "python" / "1"
            hidden = tool_dir / "hidden"
            hidden.mkdir(parents=True)
            (hidden / "f").write_text("x")
            for path in (tool_dir, tool_dir.parent):
                path.chmod(0o555)
            hidden.chmod(0o111)
            try:
                with mock.patch.object(toolchain, "TOOLCHAIN", Path(d)):
                    for call in (toolchain.writable_paths, toolchain.tree_digest):
                        with self.subTest(call=call.__name__):
                            with self.assertRaisesRegex(toolchain.ToolchainError, "can't read"):
                                call(tool_dir)
            finally:
                for path in (tool_dir.parent, tool_dir, hidden):
                    path.chmod(0o755)

    def test_ownership_errors_in_the_install_command_are_reported_not_raised(self):
        # PR #83 review: after a past `sudo make toolchain`, relinking in a root-owned
        # .toolchain/bin raised a bare PermissionError.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "load_lock", lambda: {"python": {"version": "1"}}), \
                mock.patch.object(toolchain, "platform_key", lambda: "linux-x86_64"), \
                mock.patch.object(toolchain, "install_tool", lambda *a, **k: Path(d) / "python" / "1" / "bin"), \
                mock.patch.object(toolchain, "link", side_effect=PermissionError(13, "Permission denied", "bin/python3")):
            args = argparse.Namespace(approved=True, only=["python"], include=None, force=False)
            with self.assertRaisesRegex(toolchain.ToolchainError, "another user"):
                toolchain.cmd_install(args)

    def test_a_symlinked_bin_directory_is_refused_before_any_link_change(self):
        # PR #83 round 2: link() followed a symlinked .toolchain/bin and replaced files outside it.
        with tempfile.TemporaryDirectory() as d:
            tc, outside = Path(d) / ".toolchain", Path(d) / "outside"
            tc.mkdir()
            outside.mkdir()
            (outside / "python3").write_text("not ours")
            (tc / "bin").symlink_to(outside)
            with mock.patch.object(toolchain, "TOOLCHAIN", tc), mock.patch.object(toolchain, "BIN", tc / "bin"):
                with self.assertRaisesRegex(toolchain.ToolchainError, "not a real directory"):
                    toolchain.link(tc / "python" / "1" / "bin" / "python3", "python3")
                blob, spec = self.tool_spec()
                with self.assertRaisesRegex(toolchain.ToolchainError, "not a real directory"):
                    toolchain.verify_tool("python", {"python": spec}, "linux-x86_64")
            self.assertEqual((outside / "python3").read_text(), "not ours")

    def test_download_failures_keep_their_cause_and_are_not_called_ownership(self):
        # PR #83 round 2: a TLS or network failure was reported as "created by another user".
        import ssl
        import urllib.error
        for err in (urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed")),
                    urllib.error.URLError("Name or service not known"), TimeoutError("timed out")):
            with self.subTest(err=repr(err)), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(urllib.request, "urlopen", side_effect=err):
                with self.assertRaises(toolchain.ToolchainError) as cm:
                    toolchain.download("https://example.invalid/x", Path(d) / "x")
                self.assertIn("download failed: https://example.invalid/x", str(cm.exception))
                self.assertNotIn("another user", str(cm.exception))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d) / ".toolchain"), \
                mock.patch.object(toolchain, "BIN", Path(d) / ".toolchain" / "bin"), \
                mock.patch.object(toolchain, "load_lock", lambda: {"python": {"version": "1"}}), \
                mock.patch.object(toolchain, "platform_key", lambda: "linux-x86_64"), \
                mock.patch.object(toolchain, "install_tool", side_effect=OSError(28, "No space left on device")):
            args = argparse.Namespace(approved=True, only=["python"], include=None, force=False)
            with self.assertRaises(OSError) as cm:  # not rewritten as an ownership problem
                toolchain.cmd_install(args)
            self.assertNotIsInstance(cm.exception, toolchain.ToolchainError)

    def test_a_failed_hardening_that_cannot_be_undone_still_reports_the_original_error(self):
        # PR #83 review: the cleanup after a failed hardening could itself raise a bare OSError
        # and hide the real error.
        blob, spec = self.tool_spec()
        real_chmod = Path.chmod

        def refuse_removing_write_bits(path, mode, *a, **k):
            if not mode & 0o200:
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *a, **k)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)), \
                mock.patch.object(Path, "chmod", refuse_removing_write_bits), \
                mock.patch.object(Path, "unlink", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaisesRegex(toolchain.ToolchainError, "can't make .* read-only"):
                toolchain.install_tool("python", spec, "linux-x86_64")

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

    def npm_tarball(self, data: bytes) -> tuple[bytes, str]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("package/pnpm")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        import base64
        return blob, "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()

    def test_npm_tarball_is_checked_by_integrity_and_wrapped_with_pinned_node(self):
        blob, integrity = self.npm_tarball(b"console.log('pnpm')\n")
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/p.tgz", "integrity": integrity,
                                                  "kind": "npm-tgz", "bin": "package/pnpm"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            path = toolchain.install_tool("pnpm", spec, "linux-x86_64")
            text = path.read_text()
            self.assertIn(str(Path(d) / "bin" / "node"), text)
            self.assertIn("package/pnpm", text)
            self.assertEqual(toolchain.install_tool("pnpm", spec, "linux-x86_64"), path)
        bad = dict(spec["linux-x86_64"], integrity="sha512-" + "A" * 86 + "==")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            with self.assertRaisesRegex(toolchain.ToolchainError, "hash mismatch"):
                toolchain.install_tool("pnpm", {"version": "1", "linux-x86_64": bad}, "linux-x86_64")

    def npm_tarball_with(self, files: dict) -> tuple[bytes, str]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        import base64
        return blob, "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()

    def pinned_pnpm_and_node(self):
        # The layout of the real pnpm 12 tarball: package.json's "bin" maps pnpm to "pnpm"
        # (registry metadata for pnpm 12.5.1), and the code lives in dist/.
        blob, integrity = self.npm_tarball_with({"package/pnpm": b"#!/usr/bin/env node\nrequire('./dist/pnpm.cjs')\n",
                                                 "package/dist/pnpm.cjs": b"console.log('pnpm')\n"})
        node = self.wrapped_tar("node-v1-linux-x64/bin/node", b"#!/bin/sh\n")
        lock = {"pnpm": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/p.tgz", "integrity": integrity,
                                                          "kind": "npm-tgz", "bin": "package/pnpm"}},
                "node": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/n.tgz",
                                                          "sha256": hashlib.sha256(node).hexdigest(),
                                                          "kind": "tar", "bin": "bin/node"}}}
        blobs = {"https://example.invalid/p.tgz": blob, "https://example.invalid/n.tgz": node}
        return lock, lambda url, dest: dest.write_bytes(blobs[url])

    def test_modified_pnpm_code_is_detected_not_just_the_wrapper(self):
        # PR #7 review, round 4: only the generated wrapper used to be hashed.
        lock, fake_download = self.pinned_pnpm_and_node()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", fake_download):
            for name in ("node", "pnpm"):
                toolchain.link(toolchain.install_tool(name, lock[name], "linux-x86_64"), name)
            toolchain.verify_tool("pnpm", lock, "linux-x86_64")
            code = Path(d) / "pnpm" / "1" / "package" / "dist" / "pnpm.cjs"
            code.chmod(0o644)  # installs are read-only; tampering has to undo that first
            code.write_bytes(b"steal()\n")
            with self.assertRaisesRegex(toolchain.ToolchainError, "pnpm was modified"):
                toolchain.verify_tool("pnpm", lock, "linux-x86_64")
            toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")  # reinstalls from the artifact
            self.assertEqual(code.read_bytes(), b"console.log('pnpm')\n")
            toolchain.verify_tool("pnpm", lock, "linux-x86_64")
            # The Node that runs pnpm is part of pnpm's verification.
            (Path(d) / "node" / "1" / "node-v1-linux-x64").chmod(0o755)  # undo the read-only install
            (Path(d) / "node" / "1" / "node-v1-linux-x64" / "lib").mkdir()
            with self.assertRaisesRegex(toolchain.ToolchainError, "node was modified"):
                toolchain.verify_tool("pnpm", lock, "linux-x86_64")

    def test_moved_checkout_is_reinstalled_not_reused(self):
        # PR #7 review, round 4: the wrapper embeds absolute paths of the original tree.
        lock, fake_download = self.pinned_pnpm_and_node()
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / "a" / ".toolchain", Path(d) / "b" / ".toolchain"
            with mock.patch.object(toolchain, "TOOLCHAIN", old), mock.patch.object(toolchain, "BIN", old / "bin"), \
                    mock.patch.object(toolchain, "download", fake_download):
                old.mkdir(parents=True)
                toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")
            shutil.copytree(old, new, symlinks=True)
            with mock.patch.object(toolchain, "TOOLCHAIN", new), mock.patch.object(toolchain, "BIN", new / "bin"), \
                    mock.patch.object(toolchain, "download", fake_download):
                with self.assertRaisesRegex(toolchain.ToolchainError, "not installed"):
                    toolchain.verify_tool("pnpm", lock, "linux-x86_64")
                path = toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")
                self.assertNotIn(str(old), path.read_text())
                self.assertIn(str(new / "bin" / "node"), path.read_text())

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
