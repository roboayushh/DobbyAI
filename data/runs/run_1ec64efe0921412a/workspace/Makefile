# ============================================================
# AI Coding Harness – Phase 1
# ============================================================

PYTHON   ?= python3.12
VENV     := .venv
BIN      := $(VENV)/bin
PIP      := $(BIN)/pip
HARNESS  := $(BIN)/harness

.PHONY: setup run test smoke clean lint

# ----------------------------------------------------------
# AC01 – fresh environment setup
# ----------------------------------------------------------
setup:
	@echo "🔧  Creating virtual environment …"
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements.txt -r requirements-dev.txt
	$(PIP) install --quiet -e .
	@ln -sfn $$(pwd)/src/harness $(VENV)/lib/*/site-packages/harness 2>/dev/null || true
	@echo "✅  Setup complete.  Run:  make run"

# ----------------------------------------------------------
# Interactive intake
# ----------------------------------------------------------
run:
	@$(PIP) install -e . -q
	@$(HARNESS) run $(ARGS)

# ----------------------------------------------------------
# Non-interactive commands (documented interface)
# ----------------------------------------------------------
issues:
	@$(HARNESS) issues $(ARGS)

issue:
	@$(HARNESS) issue $(ARGS)

cache-clear:
	@$(HARNESS) cache clear

# ----------------------------------------------------------
# Test suite (mocked, no live network required)
# ----------------------------------------------------------
test:
	$(BIN)/pip install -e . -q
	$(BIN)/pytest tests/ --cov=src/harness --cov-report=term-missing $(ARGS)

# ----------------------------------------------------------
# Smoke test (PRD 1 requirement FND-023)
# ----------------------------------------------------------
smoke:
	@$(BIN)/python tests/smoke_test.py

# ----------------------------------------------------------
# Lint
# ----------------------------------------------------------
lint:
	$(BIN)/python -m py_compile $$(find src tests -name "*.py")
	@echo "✅  Syntax OK"

# ----------------------------------------------------------
# Clean – preserves saved snapshots in data/snapshots/
# ----------------------------------------------------------
clean:
	rm -rf $(VENV) __pycache__ .pytest_cache .coverage *.egg-info src/*.egg-info build dist
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "🧹  Clean.  (data/snapshots/ preserved)"
