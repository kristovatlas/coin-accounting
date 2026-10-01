---
status: accepted
date: 2026-10-01
deciders: repository owner (human), drafted by Claude Code
---

# 0024: Prebuilt native-code wheels for four development tools

## Context and Problem Statement

ENGINEERING §4.1 requires an ADR for "a new dependency with network, native-code or install-time execution capability". The next development tools on the approved-to-propose list in `docs/DEPENDENCIES.md` all ship compiled code. Checked against PyPI's file lists on 2026-10-01, for the versions about to be proposed:

| Tool (version) | Native part | Pure-Python wheel? |
|---|---|---|
| `hypothesis` 6.168.1 | compiled extension (`cp310-abi3` and `cp313` wheels) | **no** |
| `ruff` 0.16.8 | the whole tool is a Rust binary inside the wheel (`py3-none-<platform>`) | **no** |
| `mypy` 2.3.1 | mypyc-compiled modules (`cp313` wheels) | yes, but uv installs the platform wheel ahead of it |
| `coverage` 7.16.1 | the C tracer (`cp313` wheels) | yes, but uv installs the platform wheel ahead of it |

Native code is harder to review than Python: Socket's analysis and a human reading the diff see much less of it. But it doesn't add a new *kind* of capability. Any Python dependency already runs arbitrary code in-process (ADR 0012, R-4). What ADR 0013 rules out is **building** code at install time (`no-build = true`), not running prebuilt wheels.

## Considered Options

1. **Allow prebuilt native wheels for these four development tools**, under the existing controls, with no other change.
2. **Pure-Python only.** This drops `ruff` and `hypothesis`, the property-test and lint tools ENGINEERING §3 and §5 are written around. It would also mean pinning `mypy` and `coverage` to their pure wheels, which uv can't express per package while `no-build` is on.
3. **An ADR per tool.** That's four near-identical records.

## Decision Outcome

Chosen option: **1**. Prebuilt native-code wheels are allowed for **`hypothesis`, `coverage`, `ruff` and `mypy`**, as **development dependencies only** (the `dev` group, never imported by `backend/coinacct`).

This also covers the native-code packages these four **require**, as long as their own project publishes them for that tool. Each one is named in its tool's proposal PR and register row. At the time of writing that is `librt` (the mypyc runtime library) and `ast-serialize` (mypy's AST serializer), both from the mypy project and required by mypy 2.3.1. A native package from anyone else, pulled in transitively, needs its own ADR.

The following still apply unchanged:
- every version is proposed in its own PR with `make propose-py`, past the 7-day cooldown
- the Socket report and the human's approval of the lockfile diff (ENGINEERING §2.4)
- `no-build = true`: wheels only, never an sdist build
- the lockfile policy check (registry source, sha256, age)
- the `.pth` allowlist check after every install

Any **other** native-code dependency still needs its own ADR. That includes any **runtime** one (e.g. `pydantic-core` under `fastapi`), whose code would run inside the app against real data.

### Consequences

- Good: the test, lint and type tools in ENGINEERING §3 and §5 can be installed, and one record covers them.
- Good: the boundary is explicit. Development tools only, prebuilt only, these four named.
- Bad: these tools' compiled parts are effectively trust-on-publisher. Socket and review see less of them than of Python source. They run on the development machine, where R-6 (dev work and real data on one machine) already applies.
- Bad: a future version of one of these tools could add a network or install-time feature. Each version bump goes through §2.4 again, and the reviewer should look for that.

## References

- ENGINEERING §2.2 (Python controls), §2.4 (vetting), §4.1 (when an ADR is required)
- ADR 0012 (in-process egress accepted), ADR 0013 (supply-chain policy)
- THREAT_MODEL T-601, T-602, R-4, R-6
- `docs/DEPENDENCIES.md`, "Proposed, awaiting approval" (Python, development)
