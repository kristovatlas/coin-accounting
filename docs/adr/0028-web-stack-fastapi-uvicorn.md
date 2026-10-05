---
status: accepted
date: 2026-10-02
deciders: repository owner (human), drafted by Claude Code
---

# 0028: FastAPI and uvicorn as the runtime web stack, with pydantic-core's native wheels

## Context and Problem Statement

ADR 0002 makes the app a local web app, and architecture §1 and §3 name **FastAPI** for the API and **uvicorn** run in-process by the launcher. These are the first **runtime** dependencies: they run with the user's real data. ENGINEERING §4.1 requires an ADR because:

- **Native code:** FastAPI needs `pydantic`, which needs **`pydantic-core`**. That is a compiled Rust extension with no pure-Python wheel, and it is a runtime dependency. ADR 0024 allowed native wheels only for four development tools.
- **Network capability:** uvicorn opens the server socket for flow F1. It binds loopback only (architecture §4, §5).

Two facts shape the versions, both checked against PyPI on 2026-10-02:

- **FastAPI 0.142.0 (2026-09-29) added a hard dependency on `opentelemetry-api`.** On its own the API package records and exports nothing; an SDK and an exporter are separate optional extras. But a telemetry library in the runtime is what THREAT_MODEL T-604 warns about. **FastAPI 0.141.1 (2026-07-29)** is the newest release without it. It is also the newest release past the 7-day cooldown.
- **uvicorn 0.54.0 (2026-09-25)** is inside the cooldown. **0.53.0 (2026-09-14)** is the newest past it.

## Considered Options

1. **FastAPI 0.141.1 + uvicorn 0.53.0 (plain, no `[standard]`), allowing `pydantic-core`'s native wheels at runtime.** Stay on FastAPI releases without `opentelemetry-api` until a later decision.
2. **Starlette + uvicorn without FastAPI.** No pydantic, so no native code, at the cost of writing request validation by hand. DEPENDENCIES.md already lists this as the alternative.
3. **The newest FastAPI (0.142.x), accepting `opentelemetry-api`,** once it is past the cooldown.

## Decision Outcome

Proposed: **1**. The human decides between 1 and 2 when approving this PR.

- **Packages:** `fastapi` 0.141.1, `uvicorn` 0.53.0 and what they require: `starlette` 1.7.0, `pydantic` 2.13.5, **`pydantic-core` 2.46.5 (native)**, `anyio` 4.15.1, `idna` 3.20, `annotated-doc` 0.0.5, `annotated-types` 0.8.0, `typing-inspection` 0.4.4 (with `typing-extensions` 4.16.0, already locked), `click` 8.5.0 and `h11` 0.16.0. All are MIT or BSD-3-Clause, per each wheel's metadata.
- **Native code at runtime** is allowed for `pydantic-core`, which comes from the pydantic project and is required by `pydantic`. Any other native runtime package needs its own ADR.
- **uvicorn is plain:** without `[standard]`, so no `uvloop`, `httptools`, `watchfiles` or `websockets`. It runs in-process. Its reload, worker and multiprocess modes and its command line aren't used (architecture §3: one process, no child processes).
- **No `opentelemetry-api`:** a FastAPI upgrade that requires it, such as Dependabot's next grouped PR, is a supply-chain change that needs its own decision, not a routine bump.
- **Unchanged controls:**
  - `make propose-py`, the Socket report and the human's approval of the lockfile diff (§2.4)
  - the 7-day cooldown
  - `no-build`: wheels only
  - the lockfile policy check
  - the `.pth` allowlist check
  - the test-time socket guard (T-305), which covers these packages too

### Checked when proposing (2026-10-02)

- **Files:** every new wheel (12 packages, including all 15 `pydantic-core` platform wheels) matches its `uv.lock` hash, and none contains a `.pth` file or `.data/scripts`.
- **Network and process calls** in the pure-Python sources, read statically: `fastapi` and `starlette` use `http.client` for HTTP status phrases. Starlette's `TestClient` needs `httpx`, which isn't installed. `pydantic/_internal/_git.py` runs `git` only for `version_info()` from a source checkout. `anyio` is a general networking and process library, used only when called. `click`'s `webbrowser` and `subprocess` uses are terminal helpers. `uvicorn`'s `subprocess` uses are its reload and worker modes. None of them opens a connection or a process on import.
- **Not checked:** `pydantic-core`'s compiled code, beyond its hashes. That is the residual risk this ADR accepts.

### Consequences

- Good: the API layer the architecture describes, with declarative request validation (T-105, T-111), and no telemetry package in the runtime.
- Bad: compiled Rust code runs with the user's data (R-4). Socket and human review see less of it than of Python. This ADR accepts that for `pydantic-core` only.
- Bad: FastAPI stays on 0.141.x until the `opentelemetry-api` question is decided.

## References

- ADR 0002 (local web app), ADR 0012 (in-process egress accepted), ADR 0013 (supply-chain policy), ADR 0024 (native-code dev tools)
- Architecture §1, §3, §5; THREAT_MODEL T-305, T-601, T-602, T-604
- ENGINEERING §2.4, §4.1
