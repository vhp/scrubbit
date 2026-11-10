# Development and install tasks for scrubbit, driven by uv.
.PHONY: help dev run test lint fmt check install uninstall package clean

help:
	@echo "make dev        create .venv with scrubbit and dev tools (make clean removes it)"
	@echo "make install    install the scrubbit command via uv tool"
	@echo "make uninstall  remove that install"
	@echo "make package    build a wheel and sdist into dist/"
	@echo "make check      fmt, lint and test; run before committing"
	@echo "make test | lint | fmt | clean | run ARGS='--dry-run'"

dev:
	uv sync

run:
	uv run scrubbit $(ARGS)

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

fmt:
	uv run ruff check --fix --exit-zero src tests
	uv run ruff format src tests

check: fmt lint test

install:
	uv tool install --force .

uninstall:
	uv tool uninstall scrubbit

package:
	rm -rf dist
	uv build

clean:
	rm -rf .venv dist build *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
