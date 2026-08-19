.PHONY: install test lint typecheck doctor serve export coverage

install:
	pip install -e ".[dev]"

test:
	python3 -m pytest tests/ -q

coverage:
	python3 -m pytest tests/ -q --cov=maxpreps_broadcast --cov-report=term-missing

lint:
	ruff check src/ tests/

typecheck:
	mypy src/maxpreps_broadcast

doctor:
	python3 -m maxpreps_broadcast.cli doctor

serve:
	python3 -m maxpreps_broadcast.cli serve --watch

export:
	python3 -m maxpreps_broadcast.cli export
