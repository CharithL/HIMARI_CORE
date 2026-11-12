.PHONY: setup lint test backtest shadow dashboard clean

VENV := .venv
UV := uv

setup:
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install -e ".[dev]"
	$(UV) pip freeze > requirements-lock.txt

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/
	mypy src/himari/core src/himari/risk src/himari/strategy src/himari/execution

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
