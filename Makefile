# base-rate developer targets. Every target named in docs/BUILD_BRIEF.md
# section 10.1 step 7 exists from day one; targets whose tools have not been
# built yet print NOT IMPLEMENTED and fail, so no gate can pass by accident.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= 3.11
RUN := uv run --frozen
COV_JSON ?= runs/coverage.json
JUNIT_XML ?= runs/junit.xml
DEMO_OUT ?= runs/demo_output.txt

.PHONY: help setup doctor test lint typecheck ci demo prereg-verify claimcheck \
	freshness citecheck secrets-scan budget review report reproduce ship-check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

setup: ## Create the locked environment (PYTHON=3.11 by default)
	uv sync --frozen --python $(PYTHON)

doctor: ## Detect hardware, backends, sandboxes, and the billing guard
	python3 tools/doctor.py --json runs/doctor.json

test: ## Run tests with coverage (COV_JSON, JUNIT_XML set report paths)
	@mkdir -p $(dir $(COV_JSON)) $(dir $(JUNIT_XML))
	$(RUN) pytest --cov=baserate --cov-report=term-missing \
		--cov-report=json:$(COV_JSON) --junitxml=$(JUNIT_XML)

lint: ## ruff check and ruff format --check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

typecheck: ## mypy --strict on the package
	$(RUN) mypy --strict src/baserate

ci: lint typecheck test demo secrets-scan ## What CI runs: lint, typecheck, test, offline demo

demo: ## Offline demo on recorded fixtures; output must match demo/golden_output.txt
	@echo "NOT IMPLEMENTED: make demo (milestone M1, docs/PLAN.md)"; exit 1

prereg-verify: ## Verify the preregistration chain
	python3 tools/prereg.py verify

claimcheck: ## Every number in README.md and docs/REPORT.md traces to an artifact
	python3 tools/claimcheck.py README.md docs/REPORT.md

freshness: ## Re-derive every live number in README.md and docs/REPORT.md
	python3 tools/freshness.py README.md docs/REPORT.md

citecheck: ## Validate artifacts/citecheck.json offline
	@echo "NOT IMPLEMENTED: make citecheck (milestone M2, docs/PLAN.md)"; exit 1

secrets-scan: ## Scan tracked files and history for secrets
	python3 tools/secrets_scan.py

budget: ## Show cash_usd and whether every cap was respected
	$(RUN) baserate budget

review: ## Independent review (headless opus, else the reviewer subagent)
	python3 tools/review.py

report: ## Build figures and tables from banked artifacts
	@echo "NOT IMPLEMENTED: make report (milestone M5, docs/PLAN.md)"; exit 1

reproduce: ## Rebuild every figure and table from banked artifacts
	@echo "NOT IMPLEMENTED: make reproduce (milestone M5, docs/PLAN.md)"; exit 1

ship-check: ## Gates G1 to G14 on a fresh clone (full run; the only one that counts)
	python3 tools/ship_check.py
