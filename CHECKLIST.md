# CHECKLIST.md

Сопоставление 17 блоков технического задания с файлами и честным статусом реализации.

Легенда: ✅ реализовано и покрыто тестами · ⚠️ реализовано с оговоркой (оговорка указана)

---

## 1. Архитектура проекта — ✅

Чистая модульная структура: backend/API, БД, адаптеры источников, адаптеры участников,
risk engine, paper execution, статистика, web-интерфейс, тесты, документация.
Архитектурная схема — в README (раздел «Архитектура»).

| Слой | Файлы |
|------|-------|
| backend/API | `app/main.py`, `app/api/routes/{markets,rounds,portfolios,stats,export,ui}.py`, `app/api/schemas.py` |
| база данных | `app/db/base.py`, `app/db/models.py`, `alembic/` |
| адаптеры источников | `app/adapters/market_data/{base,mock,polymarket,factory}.py` |
| адаптеры участников | `app/adapters/participants/{base,prompting,llm_base,codex,claude,titan,mock_brain,factory}.py` |
| risk engine | `app/services/risk_engine.py` |
| paper execution | `app/services/paper_engine.py`, `app/services/book.py` |
| статистика | `app/services/stats.py` |
| web-интерфейс | `app/web/templates/*.html`, `app/web/static/*` |
| тесты | `tests/` (123 теста) |
| документация | `README.md`, `IMPLEMENTATION_PLAN.md`, этот файл |

---

## 2. Участники и раздельные банки — ✅

Три независимых портфеля `codex`, `claude`, `titan`, стартовый баланс каждого $1000 USDC.
Участники не видят решения друг друга до фиксации собственного.

- `app/db/models.py::Participant, Portfolio, LedgerEntry`
- `app/services/seed.py::seed_participants`
- `app/services/portfolio.py` — резервы, списания, экспозиция
- Тесты: `tests/test_isolation.py::test_three_independent_portfolios`,
  `test_debiting_one_bank_does_not_touch_others`, `test_negative_balance_is_impossible`

---

## 3. Единый снимок данных — ✅

Immutable snapshot со всеми требуемыми полями: ID и название рынка, команды, тип рынка,
время начала матча, цены YES/NO, ликвидность, bid/ask, доступная глубина, последние изменения
цены, время получения данных, контекст оператора (составы/турнир).
Payload неизменяем, SHA-256 хранится в `snapshots.payload_hash`.

- `app/schemas/snapshot.py::MarketSnapshot` (frozen-модель, `content_hash`, `to_prompt_dict`)
- `app/services/snapshots.py::capture_snapshot`
- Все три участника получают один и тот же объект: `app/services/rounds.py::snapshot_model_for`
- Тесты: `tests/test_schemas.py` (хеш, TTL, состав полей),
  `tests/test_isolation.py::test_prompts_are_identical_except_own_bank`

---

## 4. Получение рынков Polymarket — ✅

Read-only адаптер публичных API (Gamma `/markets`, CLOB `/book`): поиск рынков Dota 2 /
The International, нормализация событий, сохранение сырых ответов, обработка исчезновения и
изменения рынка. Реальные ордера не отправляются (`supports_live_orders = False`, методов
отправки нет). Есть mock-провайдер с 5 демо-матчами — проект запускается без сети.

- `app/adapters/market_data/polymarket.py`, `mock.py`, `base.py`, `factory.py`
- `app/db/models.py::RawMarketPayload` — сырые ответы
- `MarketNotAvailable` → рынок помечается `UNAVAILABLE`, событие в аудит
- Тесты: `tests/test_adapters.py` (нормализация через `httpx.MockTransport`, сетевая ошибка,
  отсутствующий рынок, read-only), `tests/test_no_live_trading.py`

---

## 5. Формат Trade Decision — ✅

Строгая Pydantic-схема со всеми полями ТЗ: `participant_id`, `snapshot_id`, `market_id`,
`action` (BUY_YES/BUY_NO/SELL/HOLD), `estimated_probability`, `market_probability`, `edge`,
`stake_usdc`, `max_acceptable_price`, `confidence` 0–1, `short_reason`, `key_factors`,
`risk_factors`, `information_used`, `created_at`, `model_name`, `model_version`, `prompt_version`.
`extra="forbid"`, кросс-поле-валидация (HOLD ⇒ stake=0; BUY ⇒ max_price обязателен).
Невалидный ответ сохраняется со статусом `INVALID`/`FAILED` и отклоняется risk engine —
исполнить его нельзя.

- `app/schemas/decision.py`
- Тесты: `tests/test_schemas.py` (12 кейсов), `tests/test_export_and_audit.py::test_api_errors_are_logged`

---

## 6. Адаптер Codex — ✅

Формирование промпта из snapshot, запрос структурированного JSON (`response_format:
json_object`), проверка схемы, повторный запрос при невалидном формате, таймаут и обработка
ошибок, сохранение модели/версии/версии промпта, mock-режим без ключа.
Решение другим участникам не раскрывается.

- `app/adapters/participants/codex.py`, `llm_base.py`, `prompting.py`, `mock_brain.py`
- Тесты: `tests/test_adapters.py::test_llm_adapter_{parses_valid_response,retries_on_invalid_json,
  retries_on_schema_violation,gives_up_after_retries,handles_timeout}`

---

## 7. Адаптер Claude — ✅

Тот же формат TradeDecision, тот же snapshot, те же ограничения, structured JSON, валидация,
retry, timeout, mock-режим, сохранение версии модели и промпта.
Никаких дополнительных данных относительно Codex: промпты строит один общий модуль, тест
сравнивает их посимвольно (различается только строка с ключом собственного банка).

- `app/adapters/participants/claude.py` + общий `llm_base.py` и `prompting.py`
- Тесты: `tests/test_isolation.py::test_prompts_are_identical_except_own_bank`,
  `tests/test_adapters.py`

---

## 8. Ручной интерфейс Титана — ✅

Страница со snapshot и формой: BUY YES / BUY NO / SELL / HOLD, собственная оценка вероятности,
сумма, максимальная цена, уверенность, краткое объяснение, ключевые и риск-факторы.
После отправки решение блокируется (`locked=True`, UNIQUE `(round_id, participant_id)`,
повторная отправка → HTTP 409). Решения ИИ на странице не отображаются.

- `app/web/templates/titan.html`, `app/api/routes/ui.py::titan_page`
- `app/api/routes/rounds.py::submit_decision`, `app/services/rounds.py::submit_manual_decision`
- Тесты: `tests/test_isolation.py::test_titan_page_does_not_contain_ai_decisions`,
  `tests/test_rounds_flow.py::test_decision_cannot_be_resubmitted`

---

## 9. Цикл принятия решений — ✅

Оператор выбирает рынок → snapshot → одновременный запрос Codex и Claude → ожидание Titan →
валидация трёх решений → risk engine → симуляция исполнения → только затем раскрытие.
Статусы раунда: `OPEN → LOCKED → EXECUTED → REVEALED` (+ `CANCELLED`).
Защита от повторного исполнения: проверка статуса + UNIQUE `simulated_orders.decision_id`.

- `app/services/rounds.py` (весь модуль), `app/api/routes/rounds.py`
- Одновременность ИИ: `ThreadPoolExecutor` в `request_ai_decisions`
- Тесты: `tests/test_rounds_flow.py` (13 тестов), `tests/test_integration_round.py`

---

## 10. Прематч и live-фазы — ✅

Поддерживаются `PREMATCH` и `BETWEEN_MAPS`. Автоматических ставок внутри карты нет — других
фаз в перечислении не существует. Для `BETWEEN_MAPS` оператор вручную подтверждает окончание
карты и создаёт новый snapshot; предыдущий помечается `superseded_by_id`, и решения по нему
отклоняются как устаревшие.

- `app/constants.py::Phase`, `app/api/routes/rounds.py::create_between_maps_round`
- `app/services/snapshots.py::_supersede_previous`, `is_stale`
- Тесты: `tests/test_rounds_flow.py::test_decisions_on_stale_snapshot_are_rejected`,
  `test_new_snapshot_supersedes_previous`, `test_between_maps_requires_map_number`,
  `tests/test_integration_round.py::test_between_maps_flow_invalidates_previous_snapshot`

---

## 11. Risk engine — ✅

Детерминированные ограничения, одинаковые для всех: максимум 10% банка на позицию, 25% на матч,
50% в открытых позициях, запрет по устаревшему snapshot, запрет отрицательного баланса, запрет
дублирования исполнения, минимальная ликвидность, максимальное проскальзывание, допустимый
HOLD, журнал причин изменения или отклонения. Все лимиты в config с безопасными значениями
по умолчанию.

- `app/services/risk_engine.py` (чистая функция `evaluate` + слой БД), `app/config.py::RiskLimits`
- Журнал: `risk_evaluations.reasons`, а также `decision_before` / `decision_after`
- Тесты: `tests/test_risk_engine.py` (17 тестов, каждый лимит отдельно)

---

## 12. Paper execution engine — ✅

Исполнение по текущему ask/bid, частичное исполнение, проскальзывание, комиссия,
резервирование денег, позиции YES/NO, закрытие позиции, отмена, расчёт средней цены,
realized и unrealized PnL, settlement после результата рынка. Симуляция воспроизводима:
случайных чисел нет, seed детерминированно выводится из `(round, participant, market, hash)`.

- `app/services/paper_engine.py`, `app/services/book.py`, `app/services/settlement.py`
- Отмена заявки: `paper_engine.cancel_order`
- Тесты: `tests/test_paper_engine.py` (15 тестов, включая `test_reproducible_fills_for_same_inputs`)

---

## 13. База данных и аудит — ✅

Сохраняются: участники, рынки, snapshots, раунды, решения, решения до и после risk engine,
simulated orders, fills, позиции, балансы (ledger), settlement, API errors, prompt/model versions,
audit events. Изменение задним числом создаёт новую запись аудита, а не переписывает старую.

В `audit_events` для решений хранится **SHA-256-отпечаток** (`decision_fingerprint`), а не
содержимое: журнал доступен по API, и он не должен раскрывать чужой ответ до фиксации;
отпечатка достаточно, чтобы доказать неизменность записи, а сверить его можно после раскрытия.

| Таблица | Модель |
|---------|--------|
| participants / portfolios / ledger_entries | банки и все движения денег |
| markets / raw_market_payloads / snapshots | рынки и сырые ответы источника |
| rounds / decisions | раунды и зафиксированные решения |
| risk_evaluations | `decision_before`, `decision_after`, `reasons`, `limits_snapshot` |
| simulated_orders / fills / positions | симуляция исполнения |
| settlements | результаты рынков |
| api_errors | ошибки провайдеров LLM |
| audit_events | append-only журнал с before/after |

- `app/db/models.py`, `app/services/audit.py`
- Версии моделей и промптов: `decisions.model_name/model_version/prompt_version`
- Тесты: `tests/test_export_and_audit.py::test_audit_events_are_append_only`,
  `test_audit_trail_records_lifecycle`

---

## 14. Статистика — ✅

Отдельно по каждому участнику: конечный банк, абсолютный и процентный PnL, ROI,
realized/unrealized PnL, win rate, число ставок, средняя ставка, максимальная просадка,
Brier score, log loss, калибровка, прибыль по prematch и between-maps, прибыль по командам и
типам рынков. Двое победителей: лучший трейдер (по банку) и лучший прогнозист (по Brier).

- `app/services/stats.py`, `app/api/routes/stats.py`
- Тесты: `tests/test_stats.py` (10 тестов, формулы проверены на эталонных значениях:
  идеальный прогноз → Brier 0, монетка → Brier 0.25 и log loss ln 2)

---

## 15. Web-панель и визуализация — ✅

Общий scoreboard трёх участников, текущие банки, доходность, открытые позиции, завершённые
ставки, история раундов, карточка матча, форма Titan, экран раскрытия решений, график изменения
банка, экспорт для монтажа. До фиксации всех решений ответы друг друга не показываются.

| Страница | Файл |
|----------|------|
| Scoreboard + график + позиции + история | `app/web/templates/dashboard.html` |
| Карточки матчей, заметки, создание раунда, результат | `markets.html` |
| Пульт раунда + snapshot + раскрытие | `round.html` |
| Форма Титана | `titan.html` |
| Экспорт | `exports.html` |

- График банков: серверный SVG без внешних библиотек (`app/services/export.py::equity_chart_svg`)
- Завершённые ставки: `GET /api/portfolios/{key}/orders`
- Тесты: `tests/test_integration_round.py::test_ui_pages_render`,
  `tests/test_isolation.py::test_round_page_hides_decisions_before_execution`

---

## 16. Экспорт для YouTube и Telegram — ✅

CSV, JSON, красивый HTML-отчёт, таблица результатов, дневной scoreboard, самые крупные
выигрыши и проигрыши, матчи с наибольшим расхождением участников, краткие объяснения решений,
график банков. Автоматической публикации нет — только создание файлов.

- `app/services/export.py`, `app/api/routes/export.py`, `python -m app.cli export`
- `POST /api/export/write` возвращает `{"published": false}` и пути к файлам
- Тесты: `tests/test_export_and_audit.py` (6 тестов)

---

## 17. Тестирование, документация и запуск — ✅

| Требование | Где |
|------------|-----|
| unit-тесты схем | `tests/test_schemas.py` |
| тесты risk engine | `tests/test_risk_engine.py` |
| тесты раздельности банков | `tests/test_isolation.py` |
| тест отсутствия утечки решений | `tests/test_isolation.py` (API, HTML-страницы, промпты, статистика, экспорт, аудит) |
| тест устаревшего snapshot | `tests/test_rounds_flow.py`, `tests/test_integration_round.py` |
| тест повторного исполнения | `tests/test_rounds_flow.py`, `tests/test_paper_engine.py` |
| тест расчёта PnL | `tests/test_paper_engine.py`, `tests/test_stats.py` |
| интеграционный тест полного раунда | `tests/test_integration_round.py` |
| seed/demo data | `app/services/seed.py`, `app/adapters/market_data/mock.py`, `app/cli.py demo` |
| .env.example без секретов | `.env.example` (+ тест, который это проверяет) |
| Dockerfile и docker-compose.yml | в корне |
| команды запуска | README «Быстрый старт», `Makefile` |
| миграции базы | `alembic/`, `alembic.ini`, `alembic/versions/*_initial_schema.py` |
| README на русском | `README.md` |
| CHECKLIST.md | этот файл |

**Результат прогона:** `123 passed`.

---

# Критерии готовности

| # | Критерий | Статус | Подтверждение |
|---|----------|--------|---------------|
| 1 | Запускается локально одной понятной последовательностью команд | ✅ | README «Быстрый старт»: `pip install -r requirements-dev.txt` → `python -m app.cli demo` → `uvicorn app.main:app` |
| 2 | Без API-ключей работает полноценный demo/mock-режим | ✅ | `MARKET_DATA_PROVIDER=mock`, адаптеры LLM переходят в mock автоматически; `tests/test_adapters.py::test_codex_and_claude_work_without_api_keys` |
| 3 | Можно создать матч и snapshot | ✅ | `/ui/markets` → «Зафиксировать snapshot», `POST /api/rounds` |
| 4 | Codex и Claude возвращают mock-решения | ✅ | `POST /api/rounds/{id}/request-ai`; демо-вывод в README |
| 5 | Titan вводит решение через интерфейс | ✅ | `/ui/rounds/{id}/titan` |
| 6 | До фиксации решений ответы участников скрыты | ✅ | `hidden: true` в API; закрыты и побочные каналы — статистика, расхождения, экспорт, аудит; `tests/test_isolation.py` (7 тестов) |
| 7 | Risk engine проверяет все три заявки | ✅ | `execute_round` создаёт `RiskEvaluation` для каждого решения; `tests/test_rounds_flow.py::test_execute_produces_orders_and_reasons` |
| 8 | Paper engine симулирует исполнение | ✅ | `simulated_orders` + `fills` + `positions`; `tests/test_paper_engine.py` |
| 9 | Scoreboard и PnL обновляются | ✅ | `/`, `GET /api/stats/scoreboard`; `tests/test_stats.py` |
| 10 | Результат рынка можно установить вручную | ✅ | кнопки на `/ui/markets`, `POST /api/markets/{id}/settle` |
| 11 | После settlement обновляются банки и статистика | ✅ | `tests/test_paper_engine.py::test_settlement_pays_winner_and_zeroes_loser`, `tests/test_stats.py::test_scoreboard_after_settlement` |
| 12 | Все основные тесты проходят | ✅ | `123 passed` |

---

# Оговорки и сознательные ограничения

Ни одно из перечисленного не является незавершённой работой — это принятые решения,
зафиксированные в документации.

1. **Live execution отсутствует физически.** Это требование ТЗ. Архитектура позволяет добавить
   `ExecutionEngine` рядом с `PaperExecutionEngine`, но сам live-режим не реализован и включить
   его конфигурацией нельзя — нужна правка кода и отдельное явное разрешение.
2. **Приватные ключи Polymarket не поддерживаются.** Для чтения рынков они не нужны,
   а подпись ордеров вне области проекта.
3. **Папка `arb_scan_stake` с ноутбука пользователя недоступна** в окружении сборки, поэтому
   готовые функции отслеживания рынков не переиспользованы. Инструкция по подключению своего
   клиента (реализовать `MarketDataProvider` и зарегистрировать в factory) — в README.
4. **Результат матча вводится оператором вручную**, автоматического получения от Polymarket нет.
5. **Аутентификации в панели нет** — рассчитана на локальный запуск. Для публичного хостинга
   нужен обратный прокси с авторизацией, особенно для страницы Титана.
6. **Ставок внутри карты в реальном времени нет** — прямое требование ТЗ.
