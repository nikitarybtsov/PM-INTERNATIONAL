.PHONY: help venv install init seed demo run test lint migrate export docker-up docker-down clean

PY ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin

help:
	@echo "make install     — создать venv и поставить зависимости"
	@echo "make init        — создать БД и трёх участников"
	@echo "make seed        — загрузить рынки (demo-набор в mock-режиме)"
	@echo "make demo        — прогнать полный демо-раунд без API-ключей"
	@echo "make run         — запустить веб-панель на http://localhost:8000"
	@echo "make test        — полный test suite"
	@echo "make lint        — ruff"
	@echo "make migrate     — применить миграции Alembic"
	@echo "make export      — сохранить экспорт для монтажа"
	@echo "make docker-up   — поднять через Docker Compose"

$(BIN)/python:
	$(PY) -m venv $(VENV)

install: $(BIN)/python
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements-dev.txt
	@echo "готово: активируйте venv командой 'source $(VENV)/bin/activate'"

init:
	$(BIN)/python -m app.cli init

seed:
	$(BIN)/python -m app.cli seed

demo:
	$(BIN)/python -m app.cli demo

run:
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	$(BIN)/python -m pytest

lint:
	$(BIN)/python -m ruff check app tests

migrate:
	$(BIN)/python -m alembic upgrade head

export:
	$(BIN)/python -m app.cli export

docker-up:
	docker compose up --build

docker-down:
	docker compose down

clean:
	rm -rf data/*.db exports/* .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
