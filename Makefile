# coin-accounting: the single entry point for installs and checks (ENGINEERING §2.3).
# Written for GNU Make 3.81+ and bash 3.2+ (macOS) as well as Linux.
# Every package install goes through Socket Firewall ($(SFW)), and there is no fallback without it.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# `override`: a command-line SFW=/TOOLBIN= must never replace the pinned tools (PR #7 review).
override TOOLBIN := $(ROOT)/.toolchain/bin

# The host interpreter that runs our scripts, including the toolchain verifier. It is
# resolved once, here, before PATH is changed, and can't be overridden from the command
# line or the environment: a substitute (e.g. SYS_PYTHON=true) would make every
# verification pass (PR #7 review). It is never the pinned python3: a caller's PATH may
# already start with .toolchain/bin (a make recipe, or `toolchain.py path`), and the pinned
# interpreter must not verify itself, so that directory and anything that is the pinned
# binary are skipped (PR #81 review, round 3).
override SYS_PYTHON := $(shell set -f; IFS=:; for d in $$PATH; do [ -n "$$d" ] && [ -x "$$d/python3" ] || continue; if [ -d "$(TOOLBIN)" ]; then [ "$$d" -ef "$(TOOLBIN)" ] && continue; [ "$$d/python3" -ef "$(TOOLBIN)/python3" ] && continue; fi; echo "$$d/python3"; break; done)
ifeq ($(SYS_PYTHON),)
$(error python3 (3.9 or newer) is required on the host, outside .toolchain/bin, to run the repository scripts)
endif

override SFW := $(TOOLBIN)/sfw
override PNPM := $(TOOLBIN)/pnpm
override UV := $(TOOLBIN)/uv
export PATH := $(TOOLBIN):$(PATH)

# uv: never sync, lock or download an interpreter implicitly (ENGINEERING §2.2).
export UV_NO_SYNC := 1
export UV_PYTHON_DOWNLOADS := never
export UV_PYTHON := $(TOOLBIN)/python3

# The lockfile check needs `tomllib` (Python 3.11+): the pinned interpreter once `make toolchain`
# has run and it verifies, otherwise the host's (CI runners have 3.12+). Chosen in the recipe, so
# the pinned tree is verified before it runs anything (#71).
override LOCK_PYTHON_PICK = py="$(SYS_PYTHON)"; if [ -x "$(TOOLBIN)/python3" ] && "$(SYS_PYTHON)" scripts/toolchain.py verify python >/dev/null 2>&1; then py="$(TOOLBIN)/python3"; fi
export UV_CACHE_DIR := $(ROOT)/.uv-cache
# pytest plugins load only when named explicitly (ENGINEERING §3.1).
export PYTEST_DISABLE_PLUGIN_AUTOLOAD := 1
# The pinned Python's tree is verified file by file, bytecode included; don't write into it.
export PYTHONDONTWRITEBYTECODE := 1

# PKG/DEV/WORKSPACE/BASE reach recipes only as environment variables and are validated or
# quoted there, never pasted into shell text (no injection).
export PKG
export DEV
export WORKSPACE
export BASE
# A package spec must be name@version (JS) or name==version (Python), nothing else.
PKG_RE := ^(@[a-z0-9._-]+/)?[A-Za-z0-9._-]+(@|==)[A-Za-z0-9.+_-]+$$

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# --- toolchain -------------------------------------------------------------

.PHONY: toolchain
toolchain: ## Install the pinned sfw, pnpm, uv, Node and Python into .toolchain/ (hash-verified; pins must be on origin/main)
	"$(SYS_PYTHON)" scripts/toolchain.py install $(if $(DEPS_OK),--approved)

.PHONY: lint-tools
lint-tools: ## Install the pinned actionlint and zizmor (hash-verified; pins must be on origin/main)
	"$(SYS_PYTHON)" scripts/toolchain.py install --only actionlint zizmor $(if $(DEPS_OK),--approved)

.PHONY: lint-workflows
lint-workflows: ## Check the GitHub workflows with the pinned actionlint and zizmor (zizmor's online audits need ZIZMOR_GITHUB_TOKEN; ADR 0026)
	@"$(SYS_PYTHON)" scripts/toolchain.py verify actionlint zizmor >/dev/null || { echo "Run 'make lint-tools' first (the pinned, unmodified actionlint and zizmor are required)." >&2; exit 1; }
	@[ -n "$${ZIZMOR_GITHUB_TOKEN:-}" ] || { echo "Set ZIZMOR_GITHUB_TOKEN to a GitHub token with no permissions (a fine-grained token with public-repository read access only): zizmor's online audits, e.g. impostor-commit, use it for rate limits (ADR 0026)." >&2; exit 1; }
	@# Both run with an empty environment plus HOME and a fixed PATH: no inherited tokens or other
	@# secrets, and no ZIZMOR_* variable can switch zizmor's online audits off or point it at another
	@# config (ADR 0026). actionlint finds .yml and .yaml workflows itself and runs no shellcheck/pyflakes.
	cd "$(ROOT)" && env -i HOME="$$HOME" PATH=/usr/bin:/bin "$(TOOLBIN)/actionlint" -no-color -shellcheck= -pyflakes=
	@# zizmor gets only its own no-permission token, and audits the whole repository (workflows,
	@# dependabot.yml and any local actions).
	cd "$(ROOT)" && env -i HOME="$$HOME" PATH=/usr/bin:/bin ZIZMOR_GITHUB_TOKEN="$$ZIZMOR_GITHUB_TOKEN" "$(TOOLBIN)/zizmor" .

.PHONY: test-tools
test-tools: ## Install the pinned bitcoind for regtest tests (hash-verified; pins must be on origin/main)
	"$(SYS_PYTHON)" scripts/toolchain.py install --only bitcoind $(if $(DEPS_OK),--approved)

# sfw and the package managers it wraps must all be the pinned, unmodified binaries.
# A missing link must not fall back to a host pnpm/uv on PATH (PR #7 review).
.PHONY: require-toolchain
require-toolchain:
	@"$(SYS_PYTHON)" scripts/toolchain.py verify sfw pnpm uv node python >/dev/null || { echo "Refusing to continue: the pinned Socket Firewall and package managers must be installed and unmodified (no fallback). Run 'make toolchain'." >&2; exit 1; }

.PHONY: require-pkg
require-pkg:
	@printf '%s\n' "$$PKG" | grep -Eq '$(PKG_RE)' || { echo "PKG must look like name@version or name==version" >&2; exit 1; }

# --- dependencies (ENGINEERING §2.4: resolve -> vet -> approve -> install) ---

.PHONY: propose-js
propose-js: require-pkg require-approved-config require-toolchain ## Resolve a JS dependency into the lockfile only. Usage: make propose-js PKG=name@version WORKSPACE=frontend|e2e [DEV=1]
	@case "$$WORKSPACE" in frontend|e2e) ;; *) echo "WORKSPACE must be frontend or e2e" >&2; exit 1;; esac
	"$(SFW)" "$(PNPM)" add --lockfile-only $${DEV:+--save-dev} --filter "./$$WORKSPACE" "$$PKG"
	@git --no-pager diff --stat -- package.json '*/package.json' pnpm-lock.yaml
	@git ls-files --others --exclude-standard -- package.json '*/package.json' pnpm-lock.yaml | sed 's/^/ new file: /'
	@echo "Nothing was installed. Next: Socket review of the lockfile diff, a DEPENDENCIES.md entry and human approval. Only the human installs unmerged changes (DEPS_APPROVED=1 on the make command line)."

.PHONY: propose-py
propose-py: require-pkg require-approved-config require-toolchain ## Resolve a Python dependency into uv.lock only. Usage: make propose-py PKG=name==version [DEV=1]
	"$(SFW)" "$(UV)" add --no-sync --no-build $${DEV:+--dev} "$$PKG"
	@git --no-pager diff --stat -- pyproject.toml uv.lock
	@git ls-files --others --exclude-standard -- pyproject.toml uv.lock | sed 's/^/ new file: /'
	@echo "Nothing was installed. Next: Socket review of the lockfile diff, a DEPENDENCIES.md entry and human approval. Only the human installs unmerged changes (DEPS_APPROVED=1 on the make command line)."

# Changes that haven't reached origin/main (i.e. aren't merged by the human) are unapproved
# (ENGINEERING §2.4). `bootstrap` refuses to install unapproved dependency files, including
# config that changes what gets installed or runs code during an install (.pnpmfile, .npmrc,
# uv.toml). `propose-*` refuse unapproved install config, because resolving follows it too;
# they also pass --no-build on the command line, where branch config can't undo it.
# `toolchain.py install` checks its own lock (so running it directly is gated too).
# After approving, the human adds DEPS_APPROVED=1 to the make command line; CI does the same
# in its workflow file (THREAT_MODEL §5.6.1). The agent guard blocks it. Paths use :(glob) so
# `*` stays within one directory, and ignored files count: a global gitignore must not hide
# them (PR #7 review, rounds 4-6).
APPROVED_REF := refs/remotes/origin/main
INSTALL_CONFIG := ':(glob)uv.toml' ':(glob)*/uv.toml' ':(glob).npmrc' ':(glob)*/.npmrc' \
	':(glob).pnpmfile.*' ':(glob)*/.pnpmfile.*' ':(glob)pnpm-workspace.yaml'
DEP_FILES := ':(glob)package.json' ':(glob)*/package.json' ':(glob)pnpm-lock.yaml' ':(glob)pyproject.toml' \
	':(glob)uv.lock' ':(glob).python-version' ':(glob).node-version' ':(glob)scripts/toolchain.lock' $(INSTALL_CONFIG)
override DEPS_OK :=
ifeq ($(origin DEPS_APPROVED),command line)
ifeq ($(DEPS_APPROVED),1)
override DEPS_OK := 1
endif
endif

# $(call approval_gate,<pathspecs>,<what>)
define approval_gate
	@if [ "$(DEPS_OK)" = 1 ]; then exit 0; fi; \
	git rev-parse -q --verify "$(APPROVED_REF)" >/dev/null || { echo "Refusing: $(APPROVED_REF) is unknown, so approval can't be checked. Run 'git fetch origin'." >&2; exit 1; }; \
	if ! git diff --quiet "$(APPROVED_REF)" -- $(1) || [ -n "$$(git ls-files --others -- $(1))" ]; then \
	  echo "Refusing: $(2) differ from origin/main, and changes are used only after the human approves them (ENGINEERING §2.4). AI agents: stop and ask the human (AGENTS.md). The human, after approving, may rerun with DEPS_APPROVED=1 on the make command line" >&2; exit 1; \
	fi
endef

.PHONY: require-approved-deps
require-approved-deps:
	$(call approval_gate,$(DEP_FILES),dependency or toolchain files)

.PHONY: require-approved-config
require-approved-config:
	$(call approval_gate,$(INSTALL_CONFIG),install config files (uv.toml, .npmrc, .pnpmfile, pnpm-workspace.yaml))

.PHONY: bootstrap
bootstrap: require-approved-deps require-toolchain ## Install exactly what the lockfiles on origin/main say, through sfw
	@if [ -f uv.lock ]; then rc=0; "$(SFW)" "$(UV)" sync --locked || rc=$$?; find .venv/lib*/python*/site-packages/__pycache__ -maxdepth 1 -iname '_virtualenv*' -delete 2>/dev/null || true; "$(SYS_PYTHON)" scripts/check_pth.py .venv; [ $$rc -eq 0 ]; else echo "no uv.lock yet (no Python dependencies approved)"; fi
	@if [ -f pnpm-lock.yaml ]; then "$(SFW)" "$(PNPM)" install --frozen-lockfile; else echo "no pnpm-lock.yaml yet (no JS dependencies approved)"; fi

.PHONY: audit
audit: ## Vulnerability audit of both lockfiles (enabled once the audit tools are approved; M0.2)
	@echo "audit: pip-audit and pnpm audit are enabled in M0.2, after their dependency approval." >&2; exit 1

# --- checks (standard library only, so they run before any dependency exists) ---

.PHONY: check
check: ## Run all repository checks
	"$(SYS_PYTHON)" scripts/check_adrs.py $${BASE:+--base "$$BASE"}
	"$(SYS_PYTHON)" scripts/check_install_commands.py
	"$(SYS_PYTHON)" scripts/check_architecture.py
	"$(SYS_PYTHON)" scripts/check_repo_files.py
	@$(LOCK_PYTHON_PICK); echo "$$py scripts/check_lockfiles.py"; "$$py" scripts/check_lockfiles.py
	"$(SYS_PYTHON)" -m unittest discover -s scripts/tests -p 'test_*.py'
	@# The lockfile tests skip on a host Python < 3.11; run them on the 3.11+ interpreter as well (#71).
	@$(LOCK_PYTHON_PICK); "$$py" -m unittest -q scripts.tests.test_check_lockfiles
