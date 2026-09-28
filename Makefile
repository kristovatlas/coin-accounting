# coin-accounting: the single entry point for installs and checks (ENGINEERING §2.3).
# Written for GNU Make 3.81+ and bash 3.2+ (macOS) as well as Linux.
# Every package install goes through Socket Firewall ($(SFW)), and there is no fallback without it.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
# `override`: a command-line SFW=/TOOLBIN= must never replace Socket Firewall (PR #7 review).
override TOOLBIN := $(ROOT)/.toolchain/bin
export PATH := $(TOOLBIN):$(PATH)

# uv: never sync, lock or download an interpreter implicitly (ENGINEERING §2.2).
export UV_NO_SYNC := 1
export UV_PYTHON_DOWNLOADS := never
export UV_PYTHON := $(TOOLBIN)/python3
export UV_CACHE_DIR := $(ROOT)/.uv-cache
# pytest plugins load only when named explicitly (ENGINEERING §3.1).
export PYTEST_DISABLE_PLUGIN_AUTOLOAD := 1

SYS_PYTHON ?= python3
override SFW := $(TOOLBIN)/sfw
# PKG/DEV reach recipes only as environment variables and are validated there, never
# pasted into shell text (no injection through PKG).
export PKG
export DEV
# A package spec must be name@version (JS) or name==version (Python), nothing else.
PKG_RE := ^(@[a-z0-9._-]+/)?[A-Za-z0-9._-]+(@|==)[A-Za-z0-9.+_-]+$$

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# --- toolchain -------------------------------------------------------------

.PHONY: toolchain
toolchain: ## Install the pinned sfw, pnpm, uv, Node and Python into .toolchain/ (hash-verified)
	$(SYS_PYTHON) scripts/toolchain.py install

.PHONY: test-tools
test-tools: ## Install the pinned bitcoind for regtest tests (hash-verified)
	$(SYS_PYTHON) scripts/toolchain.py install --only bitcoind

.PHONY: require-sfw
require-sfw:
	@$(SYS_PYTHON) scripts/toolchain.py verify sfw >/dev/null || { echo "Refusing to continue: there is no install path without the pinned Socket Firewall (no fallback)." >&2; exit 1; }

.PHONY: require-pkg
require-pkg:
	@printf '%s\n' "$$PKG" | grep -Eq '$(PKG_RE)' || { echo "PKG must look like name@version or name==version" >&2; exit 1; }

# --- dependencies (ENGINEERING §2.4: resolve -> vet -> approve -> install) ---

.PHONY: propose-js
propose-js: require-pkg require-sfw ## Resolve a JS dependency into the lockfile only. Usage: make propose-js PKG=name@version [DEV=1]
	"$(SFW)" pnpm add --lockfile-only $${DEV:+--save-dev} --filter frontend "$$PKG"
	@git --no-pager diff --stat -- package.json '*/package.json' pnpm-lock.yaml
	@echo "Nothing was installed. Next: Socket review of the lockfile diff, a DEPENDENCIES.md entry, human approval, then 'make bootstrap'."

.PHONY: propose-py
propose-py: require-pkg require-sfw ## Resolve a Python dependency into uv.lock only. Usage: make propose-py PKG=name==version [DEV=1]
	"$(SFW)" uv add --no-sync $${DEV:+--dev} "$$PKG"
	@git --no-pager diff --stat -- pyproject.toml uv.lock
	@echo "Nothing was installed. Next: Socket review of the lockfile diff, a DEPENDENCIES.md entry, human approval, then 'make bootstrap'."

.PHONY: bootstrap
bootstrap: require-sfw ## Install exactly what the lockfiles say, through sfw
	@if [ -f uv.lock ]; then $(SFW) uv sync --locked; else echo "no uv.lock yet (no Python dependencies approved)"; fi
	@if [ -f pnpm-lock.yaml ]; then $(SFW) pnpm install --frozen-lockfile; else echo "no pnpm-lock.yaml yet (no JS dependencies approved)"; fi

.PHONY: audit
audit: ## Vulnerability audit of both lockfiles (enabled once the audit tools are approved; M0.2)
	@echo "audit: pip-audit and pnpm audit are enabled in M0.2, after their dependency approval." >&2; exit 1

# --- checks (standard library only, so they run before any dependency exists) ---

.PHONY: check
check: ## Run all repository checks
	$(SYS_PYTHON) scripts/check_adrs.py $(if $(BASE),--base $(BASE),)
	$(SYS_PYTHON) scripts/check_install_commands.py
	$(SYS_PYTHON) scripts/check_architecture.py
	$(SYS_PYTHON) -m unittest discover -s scripts/tests -p 'test_*.py'
