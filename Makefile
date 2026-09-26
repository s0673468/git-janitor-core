PYTHON ?= python3
ACTIONLINT ?= actionlint

.PHONY: check lint test lint-workflows mutation
check: lint test
lint:
	$(PYTHON) -m ruff check src tests

test:
	PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests

lint-workflows:
	$(ACTIONLINT) .github/workflows/test.yml

# Deliberately manual: mutation testing is CPU-heavy and is not public CI's gate.
mutation:
	PYTHONPATH=src $(PYTHON) -m mutmut run --max-children 2
