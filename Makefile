.PHONY: setup lint test e2e report audit ci

setup:
	cd python && python -m pip install --upgrade pip && python -m pip install -e ".[dev]"

# ruff (lint and format), mypy --strict, node --check.
lint:
	cd python && python -m ruff check . ../scripts && python -m ruff format --check . ../scripts && python -m mypy
	cd js && npm run -s lint

# Python collector tests (including the end-to-end run) and the JavaScript tracker tests.
test:
	cd python && python -m pytest -q
	cd js && npm test

# The real tracker sends 200 events to the real collector on 127.0.0.1; counts are asserted.
e2e:
	python scripts/e2e.py --events 200

# Event and dead-letter counts of a collector database (ETP_DB or events.sqlite3).
report:
	python -m event_tracking_pipeline report

# Known vulnerabilities in Python and npm dependencies.
audit:
	cd python && python -m pip_audit --skip-editable --cache-dir ../.tmp/pip-audit
	cd js && npm audit

ci: setup lint test e2e audit
