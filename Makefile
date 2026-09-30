.DEFAULT_GOAL := help
COMPOSE := docker compose -f docker-compose.yml
COMPOSE_DEV := $(COMPOSE) -f docker-compose.dev.yml

.PHONY: help up db test test-unit coverage test-docker lint fmt typecheck migrate seed check requirements

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

up: ## Start the whole stack in Docker (no .env file needed)
	$(COMPOSE) up --build

db: ## Start PostgreSQL for runs on the host (publishes the port)
	$(COMPOSE_DEV) up -d --wait db

test: ## Run the tests on the host (needs `make db`)
	uv run pytest -q

test-unit: ## Run the unit tests only (no database needed)
	uv run pytest -q tests/unit

# The modules that hold the rules: services, the state machine, the policies, settlement, the
# webhook signature and the password and token code. They need a higher bar than the rest.
LOGIC_MODULES := app/*/service.py,app/bookings/state.py,app/bookings/policy.py,app/payments/settlement.py,app/payments/signature.py,app/core/security.py

coverage: ## Tests with the coverage gates that CI uses (85% overall, 90% for the logic modules)
	uv run pytest -q --cov=app --cov-report=term-missing --cov-fail-under=85
	uv run coverage report --include="$(LOGIC_MODULES)" --fail-under=90

test-docker: ## Run the tests in Docker only
	$(COMPOSE) --profile test run --rm test

lint: ## Ruff: lint and format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Ruff: fix and format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy in strict mode
	uv run mypy

migrate: ## Apply the migrations
	uv run alembic upgrade head

seed: ## Seed the admin and the sample catalog
	uv run python -m scripts.seed

check: lint typecheck test ## Lint, types and tests: the gate before every commit

requirements: ## Regenerate requirements.txt and requirements-dev.txt for reviewers without uv
	uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt
	uv export --frozen --no-hashes --no-emit-project --group dev -o requirements-dev.txt
