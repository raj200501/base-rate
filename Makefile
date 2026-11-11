# base-rate developer targets. Every target named in docs/BUILD_BRIEF.md
# section 10.1 step 7 exists; each was a failing NOT IMPLEMENTED stub until its
# tool was built, so no gate could pass by accident.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= 3.11
RUN := uv run --frozen
COV_JSON ?= runs/coverage.json
JUNIT_XML ?= runs/junit.xml
DEMO_OUT ?= runs/demo_output.txt

.PHONY: help setup doctor test lint typecheck ci demo prereg-verify claimcheck \
	freshness citecheck secrets-scan budget review review-start report reproduce ship-check

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
	@mkdir -p $(dir $(DEMO_OUT))
	BASERATE_NO_MODEL_CALLS=1 $(RUN) --offline python -m baserate.demo --out $(DEMO_OUT) > /dev/null
	diff -u demo/golden_output.txt $(DEMO_OUT)
	@echo "demo: output matches demo/golden_output.txt"

prereg-verify: ## Verify the preregistration chain
	python3 tools/prereg.py verify

claimcheck: ## Every number in README.md, docs/REPORT.md, and docs/DATA.md traces to an artifact
	python3 tools/claimcheck.py README.md docs/REPORT.md docs/DATA.md

freshness: ## Re-derive every live number in README.md, docs/REPORT.md, and docs/DATA.md
	python3 tools/freshness.py README.md docs/REPORT.md docs/DATA.md

citecheck: ## Validate artifacts/citecheck.json offline; every [@key] cited must be verified
	python3 tools/citecheck.py

secrets-scan: ## Scan tracked files and history for secrets
	python3 tools/secrets_scan.py

budget: ## Show cash_usd and whether every cap was respected
	$(RUN) baserate budget

review: ## Independent review (headless opus, else the reviewer subagent)
	python3 tools/review.py

review-start: ## Check the daily review cap, log the reviewer-subagent run, then run the reviewer subagent
	python3 tools/review.py --start-subagent

report: ## Build figures, tables, numbers, and documents from banked artifacts
	env -u BASERATE_ROOT BASERATE_NO_MODEL_CALLS=1 uv run --frozen --offline python tools/report.py

reproduce: ## Re-run E2 to E4 from banked scores and rebuild the report; nothing may change
	env -u BASERATE_ROOT BASERATE_NO_MODEL_CALLS=1 uv run --frozen --offline python tools/reproduce.py

ship-check: ## Gates G1 to G14 on a fresh clone (full run; the only one that counts)
	python3 tools/ship_check.py
