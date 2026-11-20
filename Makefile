PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install dev test fmt lint typecheck check smoke load estimate token deploy-url clean

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip

# Stamp file: dependencies are reinstalled only when a requirements file changes.
$(VENV)/.installed: requirements.txt requirements-dev.txt | $(BIN)/python
	$(BIN)/pip install -q -r requirements-dev.txt
	@touch $@

install: $(VENV)/.installed ## install runtime + dev dependencies

dev: install ## run locally with hot reload on http://127.0.0.1:8080
	$(BIN)/python -m agentlink.serve --reload

test: ## run the test suite
	$(BIN)/pytest -q -p no:warnings

fmt: ## format code
	$(BIN)/ruff format agentlink api tests scripts
	$(BIN)/ruff check --fix agentlink api tests scripts

lint:
	$(BIN)/ruff format --check agentlink api tests scripts
	$(BIN)/ruff check agentlink api tests scripts

typecheck:
	$(BIN)/mypy

check: lint typecheck test ## everything CI runs

smoke: ## start a real server and exercise it through the CLI
	bash scripts/smoke.sh

load: ## simulate 5 agents + 15 sub-agents and measure latency/RSS
	$(BIN)/python scripts/loadtest.py

estimate: ## measure bytes/message and project 24 h storage
	$(BIN)/python scripts/estimate_storage.py

token: ## print a fresh random token
	@$(BIN)/python -m agentlink.cli gen-token

deploy-url: ## print the one-click Vercel import URL for this repo's origin
	@$(BIN)/python scripts/deploy_url.py

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
