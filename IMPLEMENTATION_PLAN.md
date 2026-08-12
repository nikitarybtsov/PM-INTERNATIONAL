# IMPLEMENTATION_PLAN.md

Проект: **«Титан в Dota 2 против Codex и Claude»** — публичный YouTube-эксперимент по
paper-трейдингу на рынках The International (Polymarket).

## Ключевые принципы

1. **Только PAPER TRADING.** Реальное исполнение физически отсутствует в кодовой базе.
   Есть интерфейс `ExecutionEngine`, но единственная реализация — `PaperExecutionEngine`.
   Флаг `LIVE_TRADING_ENABLED` захардкожен в `False` и охраняется тестом.
2. **Никаких секретов в коде/БД/логах/Git.** Только переменные окружения, `.env` в `.gitignore`,
   ключи маскируются в логах и никогда не пишутся в БД.
3. **Изоляция участников.** Решения скрыты API и UI до статуса раунда `REVEALED`.
4. **Детерминизм.** Paper-исполнение воспроизводимо (seed = hash от round/participant/market).
5. **Аудит без перезаписи.** Любое изменение = новая запись в `audit_events`.

## Стек

Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · Pydantic v2 · Jinja2 + vanilla JS ·
SQLite (по умолчанию) / PostgreSQL (через `DATABASE_URL`) · Docker Compose · pytest.

## Структура

```
app/
  config.py              настройки и лимиты риск-движка
  db/                    engine, session, ORM-модели
  schemas/               Pydantic: TradeDecision, Snapshot, API DTO
  adapters/
    market_data/         MarketDataProvider: mock + polymarket (public read-only)
    participants/        ParticipantAdapter: codex (OpenAI), claude (Anthropic), titan (manual)
  services/              snapshots, rounds, risk_engine, paper_engine, settlement,
                         stats, audit, export
  api/routes/            REST + HTML-страницы
  web/                   Jinja2-шаблоны и статика
tests/                   unit + integration
alembic/                 миграции
```

## Порядок реализации (блоки ТЗ)

| # | Блок | Реализация |
|---|------|-----------|
| 1 | Архитектура | структура выше + схема в README |
| 2 | Участники и банки | `db/models.py::Participant/Portfolio`, seed по $1000 |
| 3 | Единый snapshot | `services/snapshots.py`, immutable JSON payload + хеш |
| 4 | Polymarket adapter | `adapters/market_data/{base,mock,polymarket}.py` |
| 5 | TradeDecision | `schemas/decision.py` (строгая Pydantic-схема) |
| 6 | Codex adapter | `adapters/participants/codex.py` (+ mock) |
| 7 | Claude adapter | `adapters/participants/claude.py` (+ mock) |
| 8 | Интерфейс Титана | `/ui/rounds/{id}/titan`, блокировка после отправки |
| 9 | Цикл раунда | `services/rounds.py`, конечный автомат статусов |
| 10 | PREMATCH / BETWEEN_MAPS | `phase` у snapshot, ручное подтверждение карты |
| 11 | Risk engine | `services/risk_engine.py`, детерминированные лимиты |
| 12 | Paper execution | `services/paper_engine.py` (книга заявок, частичные филлы) |
| 13 | БД и аудит | `db/models.py` + `services/audit.py` |
| 14 | Статистика | `services/stats.py` (PnL, ROI, Brier, log loss, calibration) |
| 15 | Web-панель | `app/web/templates/*` |
| 16 | Экспорт | `services/export.py` (CSV/JSON/HTML/SVG-график) |
| 17 | Тесты, доки, запуск | `tests/`, README.md, CHECKLIST.md, Docker |

## Допущения

* Доступ к папке `arb_scan_stake` на ноутбуке пользователя отсутствует в этом окружении,
  поэтому Polymarket-адаптер написан на публичный read-only Gamma/CLOB API и по умолчанию
  выключен (`MARKET_DATA_PROVIDER=mock`). Инструкция подключения — в README.
* Ключи Polymarket (приватные) **не нужны** для чтения рынков и намеренно не поддерживаются:
  подпись ордеров вне области проекта.
* Деньги считаются в USDC, округление: суммы — 2 знака, цены — 4, размеры — 6.
