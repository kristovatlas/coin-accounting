#!/usr/bin/env python3
"""Mechanical secret scan for text the review panel posts publicly (ADR 0020).

Standard library only. Reads each file (or stdin with `-`) and exits 1 if any line matches a
credential or private-key pattern: GitHub, Anthropic, OpenAI and AWS keys, JWTs (for example
Codex's login token), PEM private keys, Bitcoin Core rpcauth/rpcpassword/cookie values, and
extended private or public keys. It prints only the pattern name and the line number,
never the match. With --redact-home it also rewrites the user's home directory to `~` in
place, so local paths and usernames aren't published.

Usage:
    secret_scan.py [--redact-home] FILE...
Exit status: 0 = clean; 1 = a secret pattern matched (don't post); 2 = could not read.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - runs on the host Python
    sys.exit("secret_scan.py needs Python 3.9 or newer")

B58 = "1-9A-HJ-NP-Za-km-z"
PATTERNS = [
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    ("Anthropic or OpenAI key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("AWS access key", r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("JWT or bearer token", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Bitcoin Core RPC credential", r"\b(rpcauth|rpcpassword)\s*=\s*\S+|__cookie__:[0-9a-f]{16,}"),
    ("extended private key", rf"\b[xyztuv]prv[{B58}]{{100,}}"),
    ("extended public key (privacy)", rf"\b[xyztuv]pub[{B58}]{{100,}}"),
    ("WIF private key", rf"\b[5KLc9][{B58}]{{50,51}}\b"),
]


def findings(text: str) -> list[tuple[str, int]]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for name, pattern in PATTERNS:
            if re.search(pattern, line):
                out.append((name, n))
    return out


def redact_home(text: str) -> str:
    home = str(Path.home())
    user = os.environ.get("USER") or Path.home().name
    text = text.replace(home, "~")
    return re.sub(rf"(/home/|/Users/){re.escape(user)}\b", "~", text) if user else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--redact-home", action="store_true")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    status = 0
    for name in args.files:
        try:
            text = sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"secret_scan: {name}: {e}", file=sys.stderr)
            return 2
        for pattern, line in findings(text):
            print(f"secret_scan: {name}:{line}: {pattern}")
            status = 1
        if args.redact_home and name != "-" and status == 0:
            Path(name).write_text(redact_home(text), encoding="utf-8")
    return status


if __name__ == "__main__":
    sys.exit(main())
