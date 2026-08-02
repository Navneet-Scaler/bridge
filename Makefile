.PHONY: help setup db-up db-down db-shell schema run-sim features train-model economics nudge report test lint fmt notebook clean all

VENV := venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:  ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## Create venv (Python 3.12), install deps, install pre-commit hooks
	uv venv --seed --python 3.12 $(VENV)
	$(PIP) install -r requirements.txt
	$(VENV)/bin/pre-commit install
	@test -f .env || cp .env.example .env

db-up:  ## Start the shared Postgres container (same one Cadence uses)
	docker compose up -d postgres
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U cadence_user -d cadence >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready"

metabase-up:  ## Start Metabase (depends on postgres)
	docker compose up -d metabase

db-down:  ## Stop containers (data volume is preserved)
	docker compose down

db-shell:  ## Open a psql shell inside the container
	docker compose exec postgres psql -U cadence_user -d cadence

schema:  ## Apply sql/schema_extension.sql on top of Cadence's schema
	docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U cadence_user -d cadence < sql/schema_extension.sql

run-sim:  ## Simulate portfolios, liquidity events, and the nudge treatment/control split
	$(PY) -m src.simulate.generate_liquidity_events

features:  ## Build the propensity feature set (pulls Cadence's streak consistency)
	$(PY) -m src.modeling.feature_engineering

train-model:  ## Fit and evaluate the LAMF propensity model
	$(PY) -m src.modeling.propensity_model

economics:  ## Run the unit economics calculator and scenario table
	$(PY) -m src.economics.unit_economics_calculator

nudge:  ## Run the borrow-instead-of-withdraw treatment/control test
	$(PY) -m src.analysis.nudge_validation

report:  ## Generate the weekly loan pipeline health report
	$(PY) -m src.reporting.loan_pipeline_report

all: schema run-sim features train-model economics nudge report  ## Full pipeline, end to end

test:  ## Run the test suite
	$(VENV)/bin/pytest -q

lint:  ## Lint and format-check
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/black --check src tests

fmt:  ## Auto-format
	$(VENV)/bin/black src tests
	$(VENV)/bin/ruff check --fix src tests

notebook:  ## Launch Jupyter
	$(VENV)/bin/jupyter notebook notebooks/

clean:  ## Remove caches and generated reports
	rm -rf .pytest_cache .ruff_cache reports/ && find . -name __pycache__ -type d -prune -exec rm -rf {} +
