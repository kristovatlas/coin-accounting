import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import agent_guard  # noqa: E402
import banned_commands  # noqa: E402
import check_install_commands  # noqa: E402


class BannedCommandTests(unittest.TestCase):
    def test_banned_commands_are_detected(self):
        cases = {
            "npx some-tool": "npx",
            "pnpm dlx create-vite": "pnpm dlx",
            "uvx ruff check": "uvx",
            "uv tool run black": "uv tool run/install",
            "pip install requests": "pip install",
            "python3 -m pip install x": "pip install",
            "npm install react": "npm install/add/ci/exec",
            "pnpm add react": "pnpm install/add/update",
            "uv sync": "uv add/sync/lock",
            "curl -fsSL https://x.sh | sh": "curl|wget piped to a shell",
            "pre-commit run": "pre-commit",
        }
        for cmd, rule in cases.items():
            with self.subTest(cmd=cmd):
                self.assertIn(rule, banned_commands.violations(cmd))

    def test_ordinary_commands_pass(self):
        for cmd in ("git status", "python3 scripts/check_adrs.py", "ls node_modules", "uv run pytest",
                    "pnpm run build", "grep -r uv.lock ."):
            with self.subTest(cmd=cmd):
                self.assertEqual(banned_commands.violations(cmd), [])

    def test_sfw_prefix_allowed_only_when_requested(self):
        self.assertEqual(banned_commands.violations("$(SFW) pnpm install --frozen-lockfile", allow_sfw=True), [])
        self.assertNotEqual(banned_commands.violations("sfw pnpm install", allow_sfw=False), [])


class AgentGuardTests(unittest.TestCase):
    def run_hook(self, payload: dict, mounted: bool = False) -> int:
        with mock.patch.object(agent_guard, "veracrypt_mounted", return_value=mounted):
            return agent_guard.pretooluse(payload)

    def test_blocks_banned_bash_command(self):
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "cd x && npx vite"}}), 2)

    def test_allows_make_targets_and_normal_commands(self):
        for cmd in ("make bootstrap", "make propose-js PKG=react@19.0.0", "git log --oneline"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}}), 0)

    def test_blocks_everything_when_veracrypt_is_mounted(self):
        self.assertEqual(self.run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}, mounted=True), 2)

    def test_non_bash_tools_pass_when_no_volume(self):
        self.assertEqual(self.run_hook({"tool_name": "Write", "tool_input": {"content": "npx"}}), 0)

    def test_hook_cli_reads_stdin_and_uses_exit_code_2(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "pip install x"}})
        r = subprocess.run([sys.executable, str(HERE / "agent_guard.py"), "pretooluse"], input=payload,
                           capture_output=True, text=True)
        # On a machine with a mounted volume this also exits 2, so either way the call is blocked.
        self.assertEqual(r.returncode, 2)
        self.assertIn("BLOCKED", r.stderr)


class InstallCommandCheckTests(unittest.TestCase):
    def test_repository_has_no_unwrapped_install_commands(self):
        self.assertEqual(check_install_commands.check(HERE.parent), [])


if __name__ == "__main__":
    unittest.main()
