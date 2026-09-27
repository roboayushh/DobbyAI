# ============================================================
# AI Coding Harness (DobbyAI) – PRD 1-6
#
#   make setup    locked user-space install + sandbox runtime image (never sudo)
#   make run      interactive: model (asked only if none is set), repository, then task
#                 model:     make run MODEL=deepseek   (or qwen, qwen-coder, ...)
#                 headless:  make run INPUT=/abs/path/request.json
#                 flags:     make run ARGS='--repo ./my-repo --task "fix X"'
#   make test     the harness's deterministic test suite (not target-repo tests)
#   make doctor   prerequisite + configuration diagnosis (add ARGS=--live)
#   make clean    remove harness build/test caches only (never runs or repos)
#
# The only credential is AI_API_KEY, read from the environment at runtime
# (export AI_API_KEY=...). It is never stored, logged, or passed to the sandbox.
# ============================================================

# First interpreter >= 3.12 on PATH (override with make setup PYTHON=/path/to/python3).
ifeq ($(origin PYTHON),undefined)
PYTHON := $(shell for p in python3.12 python3.13 python3.14 python3; do \
	command -v $$p >/dev/null 2>&1 && $$p -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null \
	&& { echo $$p; break; }; done)
endif
VENV     := .venv
BIN      := $(VENV)/bin
PIP      := $(BIN)/pip
HARNESS  := $(BIN)/harness
EVIDENCE := release-evidence
# Optional model profile for this invocation: `make run MODEL=qwen` (command line only, so
# an unrelated exported MODEL variable is ignored). Otherwise HARNESS_MODEL_PROFILE is
# used, and if neither names a real profile, interactive `make run` asks once at launch.
WITH_MODEL := $(if $(filter command line,$(origin MODEL)),HARNESS_MODEL_PROFILE=$(MODEL) ,)

.PHONY: setup run test test-fast doctor clean smoke lint runtime-image sandbox-doctor sandbox-probe schemas plugins-lock sbom release-evidence bridge

# ----------------------------------------------------------
# Setup: prerequisite checks + hash-locked install (user space only)
# ----------------------------------------------------------
setup:
	@test -n "$(PYTHON)" || { echo "❌  Python >= 3.12 not found on PATH (install python3.12, or make setup PYTHON=/path/to/python3)"; exit 2; }
	@echo "🐍  Using $(PYTHON) ($$($(PYTHON) -c 'import sys; print(sys.version.split()[0])'))"
	@command -v git >/dev/null 2>&1 || { echo "❌  git not found"; exit 2; }
	@echo "🔧  Creating virtual environment …"
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet --require-hashes -r requirements.lock
	$(PIP) install --quiet --no-deps -e .
	@ln -sfn $$(pwd)/src/harness $(VENV)/lib/*/site-packages/harness 2>/dev/null || true
	@if docker info >/dev/null 2>&1; then \
		echo "🐳  Building the pinned sandbox runtime image …"; \
		$(HARNESS) sandbox build || echo "⚠️   Runtime build failed; retry with: make runtime-image"; \
	else \
		echo "⚠️   Docker is not running: model-generated actions cannot execute until it is (no host fallback)."; \
		echo "     Start Docker, then run: make runtime-image"; \
	fi
	@echo "✅  Setup complete.  Next: export AI_API_KEY=...; make run   (asks DeepSeek or Qwen; or make run MODEL=deepseek)"

# ----------------------------------------------------------
# Run: interactive by default; INPUT=request.json for headless evaluator mode
# ----------------------------------------------------------
run:
ifdef INPUT
	@$(WITH_MODEL)$(HARNESS) run --input "$(INPUT)" --non-interactive
else
	@$(WITH_MODEL)$(HARNESS) run $(ARGS)
endif

# ----------------------------------------------------------
# Tests (deterministic; Docker end-to-end tests skip when Docker is absent)
# ----------------------------------------------------------
test:
	@mkdir -p $(EVIDENCE)/tests
	$(BIN)/pytest tests/ --cov=src/harness --cov-report=term-missing --junitxml=$(EVIDENCE)/tests/junit.xml $(ARGS)

# Unit/contract tests only (no Docker end-to-end runs)
test-fast:
	$(BIN)/pytest tests/ -q --ignore=tests/test_prd345_e2e.py --ignore=tests/test_prd5_recovery.py \
		--ignore=tests/test_prd6_e2e.py $(ARGS)

doctor:
	@$(WITH_MODEL)$(HARNESS) doctor $(ARGS)

# ----------------------------------------------------------
# PRD 3 sandbox runtime (pinned image + per-machine lock)
# ----------------------------------------------------------
runtime-image:
	@$(HARNESS) sandbox build

sandbox-doctor:
	@$(HARNESS) sandbox doctor

sandbox-probe:
	@$(HARNESS) sandbox probe

# ----------------------------------------------------------
# Release maintenance
# ----------------------------------------------------------
schemas:
	@$(BIN)/python scripts/generate_schemas.py

plugins-lock:
	@$(BIN)/python scripts/generate_plugin_lock.py

sbom:
	@$(BIN)/python scripts/generate_sbom.py

release-evidence:
	@$(HARNESS) release evidence

# LOCAL DEVELOPMENT ONLY: OpenAI-compatible bridge to the claude CLI (profile claude-bridge).
# AI_API_KEY must be set to any local value; the bridge accepts only that bearer token.
bridge:
	@test -n "$$AI_API_KEY" || { echo "❌  export AI_API_KEY=<any local value> first"; exit 2; }
	@$(BIN)/python scripts/claude_openai_bridge.py $(ARGS)

smoke:
	@$(BIN)/python tests/smoke_test.py

lint:
	$(BIN)/python -m py_compile $$(find src tests -name "*.py")
	@echo "✅  Syntax OK"

# ----------------------------------------------------------
# Clean – harness build/test caches in this source tree ONLY.
# Never touches data/ (runs, snapshots), user repositories, or exports.
# Retained runs are removed with: harness clean RUN_ID (approval required).
# ----------------------------------------------------------
clean:
	rm -rf .pytest_cache .coverage htmlcov *.egg-info src/*.egg-info build dist
	find src tests scripts -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find src tests scripts -name "*.pyc" -delete 2>/dev/null || true
	@echo "🧹  Clean (harness caches only; data/ and repositories untouched)."
