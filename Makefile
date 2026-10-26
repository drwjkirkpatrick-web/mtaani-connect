# Mtaani Connect dev tasks.
# NOTE: on the Jetson dev host the DB is a walker-owned Postgres 16 (conda,
# port 5434) - `make db-start`. On generic hosts use `make up` (docker compose).
PY := .venv/bin/python
PG_CTL := $(HOME)/micromamba/envs/pg/bin/pg_ctl
PGDATA := $(CURDIR)/.pgdata

.PHONY: setup up down db-start db-stop migrate check test

setup:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

up:
	docker compose up -d

down:
	docker compose down

db-start:
	@bash scripts/db_start.sh

db-stop:
	$(PG_CTL) -D $(PGDATA) stop -m fast || true

migrate:
	$(PY) manage.py migrate

check:
	$(PY) manage.py check

test:
	$(PY) -m pytest
