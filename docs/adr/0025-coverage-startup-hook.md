---
status: proposed
date: 2026-10-01
deciders: repository owner (human), drafted by Claude Code
---

# 0025: Allow coverage.py's own start-up hook, pinned by content

## Context and Problem Statement

ENGINEERING §2.2 and THREAT_MODEL T-602 allow exactly one `.pth` file in the project's virtual environment: uv's own `_virtualenv.pth`. A `.pth` file runs code on every interpreter start. `scripts/check_pth.py` runs after every `make bootstrap` and fails on any other `.pth` file.

coverage.py 7.16.1 (proposed in PR #78) ships `a1_coverage.pth` in every one of its wheels. The file is identical (205 bytes, sha256 `ef2ed06d…48e8`) in all the locked wheels checked on 2026-10-01: Linux x86_64/aarch64 glibc, Linux aarch64 musl, macOS arm64/x86_64, and the pure `py3-none-any` wheel. It runs:

```python
import os
if os.getenv("COVERAGE_PROCESS_START") or os.getenv("COVERAGE_PROCESS_CONFIG"):
    try:
        import coverage
    except:
        pass
    else:
        coverage.process_startup(slug="pth")
```

This is how coverage measures subprocesses. ENGINEERING §3.3 requires subprocess measurement (`[run] patch = ["subprocess"]`) so the E2E runs count toward the coverage floors. Without an exception, `make bootstrap` fails once coverage is installed. CI didn't catch this because CI doesn't install dependencies yet (#44).

## Considered Options

1. **Allow exactly this file, pinned by content.**
2. **Delete the file after every install.** Subprocess measurement stops working, every `uv sync` restores the file, and coverage's `RECORD` no longer matches the disk.
3. **Don't use coverage.py yet.** That drops the coverage floors that ENGINEERING §3.3 requires.

## Decision Outcome

Chosen option: **1**. `scripts/check_pth.py` allows `a1_coverage.pth` only when all of these hold:
- its content has the pinned sha256, and it is a regular file
- an installed `coverage-*.dist-info` lists it in its `RECORD` with that same hash
- no other distribution's `RECORD` claims it, or anything named `coverage` / `coverage.*` at the top level that the hook's `import coverage` could load instead

Any other `.pth` file, and any other content for this one, still fails. A coverage version bump that changes the file fails closed until the hash in `check_pth.py` is updated. That update is reviewed with the bump.

### Consequences

- Good: the coverage floors in ENGINEERING §3.3, including E2E subprocess coverage, work without loosening the check for anything else.
- Good: the exception is narrow. One name, one exact content, one owner, checked after every install and covered by tests that use the real file.
- Bad: every Python start in the venv now reads two environment variables, and imports coverage when either is set. Any process started with `COVERAGE_PROCESS_START` or `COVERAGE_PROCESS_CONFIG` in its environment records which lines ran and writes coverage data files. This adds no new capability: whoever controls a process's environment can already run code in it, for example through `PYTHONPATH`. The app never sets these variables, and nobody should set them for a run against real data (R-6).
- Bad: one more thing to update when coverage is bumped.

## References

- ENGINEERING §2.2 (`.pth` files), §3.3 (coverage)
- THREAT_MODEL T-602, R-6
- ADR 0024 (coverage is one of the four native-code development tools)
- coverage.py docs, "Measuring subprocesses": https://coverage.readthedocs.io/en/latest/subprocess.html
