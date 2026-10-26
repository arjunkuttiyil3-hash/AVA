.PHONY: run test check

run:
	python run.py

test:
	python -m pytest -q

check:
	python scripts/check_env.py
