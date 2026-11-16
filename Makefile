.PHONY: setup lint test backtest shadow dashboard clean

VENV := .venv
UV := uv
# mypy's pure-Python build at this pinned version: Windows application control blocks
# the compiled library in mypy's standard wheel on the project PC (README, Setup note)
MYPY_VERSION := 1.20.2

setup:
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install -e ".[dev]"
	$(UV) pip install --reinstall-package mypy --no-binary mypy "mypy==$(MYPY_VERSION)"
	$(UV) pip freeze > requirements-lock.txt

lint:
	ruff check src/ tests/ scripts/
	ruff format --check src/ tests/ scripts/
	mypy src/himari/core src/himari/risk src/himari/strategy src/himari/execution src/himari/data src/himari/accounting src/himari/quality src/himari/gates src/himari/sizing src/himari/sim src/himari/research

test:
	pytest tests/ -x --cov=himari --cov-report=term-missing

test-unit:
	pytest tests/unit/ -x -v

test-property:
	pytest tests/property/ -x -v -m property

test-golden:
	pytest tests/golden/ -x -v -m golden

test-crisis:
	pytest tests/crisis/ -x -v -m crisis

backtest:
	himari backtest --config config/frozen_v1.yaml --costs 2x --from 2015-01-01 --to 2025-12-31

shadow:
	himari shadow run --broker quote_paper

dashboard:
	himari dashboard

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
