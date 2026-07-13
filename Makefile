# Makefile for the Proposal Extractor service.
#
# Targets delegate Python commands through `poetry run` so the same
# environment is used everywhere. Run `make help` for a list of
# available targets.

.PHONY: help install install-main run run-dev test test-fast format \
        lint lint-fix typecheck install-hooks precommit \
        export-reqs export-dev-reqs clean docs docs-clean docs-serve

# Default target prints the help.
.DEFAULT_GOAL := help

PACKAGE := proposal_service
SRC_DIR := src
APP := proposal_service.main:app
HOST ?= 0.0.0.0
PORT ?= 8000

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime and development dependencies
	poetry install --with dev

install-main:  ## Install only runtime dependencies (for production images)
	poetry install --only main --no-root

run:  ## Run the API with production settings (gunicorn + uvicorn workers)
	poetry run gunicorn $(APP) -c gunicorn_conf.py

run-dev:  ## Run the API with auto-reload for development
	poetry run uvicorn $(APP) --host $(HOST) --port $(PORT) --reload

test:  ## Run the full test suite with coverage
	poetry run pytest --cov=$(PACKAGE) --cov-report=term-missing

test-fast:  ## Run unit tests only (skip tests marked 'integration')
	poetry run pytest -m "not integration"

format:  ## Format the codebase with black and isort
	poetry run black $(SRC_DIR) tests
	poetry run isort $(SRC_DIR) tests

lint:  ## Lint the codebase with ruff
	poetry run ruff check $(SRC_DIR) tests

lint-fix:  ## Lint and auto-fix issues with ruff
	poetry run ruff check --fix $(SRC_DIR) tests

typecheck:  ## Type-check the codebase with mypy
	poetry run mypy $(SRC_DIR)

install-hooks:  ## Install pre-commit git hooks
	poetry run pre-commit install

precommit:  ## Run pre-commit on every tracked file
	poetry run pre-commit run --all-files

export-reqs:  ## Export runtime requirements.txt from the lock file
	poetry export --without-hashes --only main -f requirements.txt -o requirements.txt

export-dev-reqs:  ## Export development requirements-dev.txt from the lock file
	poetry export --without-hashes --with dev -f requirements.txt  -o requirements-dev.txt

clean:  ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist build *.egg-info .coverage htmlcovs


docs:  ## Build the HTML documentation into docs/build/html
	poetry run sphinx-build -b html docs/source docs/build/html

docs-clean:  ## Remove generated docs (build output + autosummary stubs)
	rm -rf docs/build docs/source/reference/generated

docs-serve: docs  ## Build then serve the docs locally on :8001
	poetry run python -m http.server 8001 --directory docs/build/html