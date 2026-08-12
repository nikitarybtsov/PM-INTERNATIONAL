# RUNBOOK — запуск на своей машине

Пошаговая инструкция для локального запуска: Codex через CLI (расход по подписке),
Claude через API, уведомления в Telegram, автоматические раунды.

Режим по-прежнему **paper trading**: реальные ордера не отправляются.

---

## 0. Что понадобится

| Что | Зачем | Где взять |
|-----|-------|-----------|
| Python 3.12 | запуск проекта | python.org или пакетный менеджер |
| Codex CLI | участник Codex | установка ниже, авторизация вашим ChatGPT-аккаунтом |
| `ANTHROPIC_API_KEY` | участник Claude | console.anthropic.com |
| Токен Telegram-бота | уведомления | @BotFather в Telegram |
| Ваш Telegram chat_id | куда слать | инструкция ниже |
| Cloudflare Tunnel (позже) | доступ Титана к форме | нужен только когда подключаете друга |

---

## 1. Установка

```bash
git clone <repo> && cd PM-INTERNATIONAL

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env
```

Проверка, что всё встало:

```bash
pytest -q          # ожидается 169 passed
python -m app.cli demo   # демо-раунд без единого ключа
```

---

## 2. Codex CLI

Установка (один раз):

```bash
npm install -g @openai/codex
codex login          # откроется браузер, войдите своим ChatGPT-аккаунтом
codex --version      # должно напечатать версию
```

Проверьте, что нужная команда действительно отдаёт текст в stdout:

```bash
echo 'Ответь ровно одним JSON-объектом: {"ok": true}' | codex exec --skip-git-repo-check
```

Если ваша версия CLI использует другие флаги — подставьте свои в `.env`:

```bash
CODEX_TRANSPORT=cli
CODEX_CLI_COMMAND=codex exec --skip-git-repo-check
```

Адаптер извлекает JSON из вывода, поэтому служебные строки вокруг ответа
не мешают. Если Codex не авторизован или флаги неверные — решение сохранится
со статусом `FAILED`, попадёт в `api_errors` и будет отклонено risk engine.
Раунд при этом не сломается.

---

## 3. Claude

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-5
```

Без ключа Claude работает заглушкой — сравнение с Codex теряет смысл.

---

## 4. Telegram

1. Напишите @BotFather → `/newbot` → получите токен.
2. Напишите своему боту любое сообщение (бот не может писать первым).
3. Узнайте chat_id:

```bash
curl "https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates"
```

В ответе найдите `"chat":{"id":123456789`. Это и есть `TELEGRAM_CHAT_ID`.

```bash
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789
```

Для группы: добавьте бота в группу, напишите там сообщение, повторите
`getUpdates` — id группы будет отрицательным. Для канала: добавьте бота
админом и укажите `@имя_канала`.

**Что приходит в Telegram:**

| Момент | Сообщение | Содержит решения? |
|--------|-----------|-------------------|
| Раунд открыт | матч, цены, ссылка для Титана | нет |
| ИИ ответили | «codex ✅ claude ✅», кого ждём | **нет** |
| За 5 мин до дедлайна | напоминание Титану | нет |
| Раунд исполнен | полное раскрытие: оценки, edge, вердикты риска, филлы | да |
| Результат матча | кто победил, банки участников | да |

Граница приватности проверяется тестами `tests/test_notifications.py` —
до исполнения раунда чужие обоснования в канал не уходят.

---

## 5. Доступ и пароли

```bash
PANEL_PASSWORD=$(python -c "import secrets; print(secrets.token_urlsafe(18))")
TITAN_ACCESS_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
```

Впишите оба в `.env`.

- Вы заходите в панель по логину `operator` и `PANEL_PASSWORD`.
- Титан получает ссылку вида `.../ui/rounds/12/titan?t=<TITAN_ACCESS_TOKEN>`.
  Этот токен открывает **только** его страницу и отправку его решения —
  скоборд, раскрытие и API статистики по нему недоступны.

Пока `PANEL_PASSWORD` пуст, авторизации нет. Это нормально для `localhost`,
но недопустимо, как только адрес доступен снаружи.

---

## 6. Живые рынки

```bash
MARKET_DATA_PROVIDER=polymarket
```

Проверьте с этой же машины, что Polymarket доступен:

```bash
curl -s "https://gamma-api.polymarket.com/markets?active=true&limit=1" | head -c 200
```

Если пусто или ошибка — скорее всего провайдер/страна блокирует домен,
понадобится VPN на машине. С `MARKET_DATA_PROVIDER=mock` всё продолжит
работать на демо-данных.

---

## 7. Автоматические раунды

```bash
SCHEDULER_ENABLED=true
SCHEDULER_INTERVAL_SECONDS=300
SCHEDULER_OPEN_BEFORE_MATCH_MINUTES=240
SCHEDULER_MIN_BEFORE_MATCH_MINUTES=20
SCHEDULER_MAX_OPEN_ROUNDS=3
TITAN_TIMEOUT_POLICY=cancel
TITAN_DEADLINE_MINUTES=15
```

Что делает планировщик каждые 5 минут:

1. обновляет список рынков Dota 2 / The International;
2. для матчей, стартующих через 20–240 минут, открывает раунд:
   фиксирует snapshot → запрашивает Codex и Claude → шлёт Титану ссылку;
3. когда Титан подал решение — прогоняет risk engine, исполняет, раскрывает,
   отправляет разбор в Telegram;
4. если Титан не успел до дедлайна — по политике `cancel` отменяет раунд.

**Титана автоматизировать нельзя.** Он человек, и «автоматический режим»
означает лишь, что система сама доводит раунд до состояния «ждём человека»
и сама разбирается с просрочкой.

Про `TITAN_TIMEOUT_POLICY`:

- `cancel` (по умолчанию) — за Титана никто решения не выдумывает.
  Раунд отменяется, его Brier score остаётся честным.
- `hold` — записывается HOLD от его имени. Раунды не пропадают, но HOLD
  попадёт в его статистику как собственное решение.

Прогнать один тик вручную, не дожидаясь таймера:

```bash
curl -u operator:ПАРОЛЬ -X POST http://localhost:8000/api/scheduler/tick
```

---

## 8. Запуск

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Проверка готовности — покажет, что настроено, а что нет:

```bash
curl -s -u operator:ПАРОЛЬ http://localhost:8000/api/doctor | python -m json.tool
```

Ожидаемый ответ при полной настройке: `"ready": true` и пустой `problems`.

Панель: http://localhost:8000

---

## 9. Доступ для Титана

Ваш друг не в вашей локальной сети, поэтому нужен туннель. Проще всего
Cloudflare Tunnel — бесплатно, без проброса портов:

```bash
# установка: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://localhost:8000
```

Команда напечатает адрес вида `https://что-то.trycloudflare.com`. Впишите его
в `.env`:

```bash
PUBLIC_BASE_URL=https://что-то.trycloudflare.com
```

и перезапустите приложение — теперь ссылки в Telegram будут внешними.

**Обязательно задайте `PANEL_PASSWORD` перед тем, как включать туннель.**

Альтернатива без публичного адреса — Tailscale: обе машины в одной приватной
сети, туннель наружу не нужен.

---

## 10. Ограничения локального запуска

| | Локально | На сервере |
|---|---|---|
| Codex CLI | уже авторизован в вашем браузере | нужна headless-авторизация |
| Настройка | 20 минут | плюс SSH, firewall, systemd |
| Работает, когда | ПК включён и не спит | всегда |
| Адрес для Титана | через туннель | постоянный домен |

Главное ограничение: **планировщик работает только пока машина включена**.
Если матч стартует ночью, а ПК спит — раунд не откроется.

Как это обойти, если оставляете на ночь:

```bash
# macOS
caffeinate -is uvicorn app.main:app --host 127.0.0.1 --port 8000

# Windows: Параметры → Питание → «Никогда» для сна
# Linux: systemd-inhibit --what=sleep uvicorn app.main:app
```

Если по итогам первых дней захочется круглосуточной работы — переезд
на Contabo делается тем же `docker compose up -d`, конфигурация не меняется.

---

## 11. Ежедневная рутина

```bash
# итог дня в Telegram
curl -u operator:ПАРОЛЬ -X POST http://localhost:8000/api/scheduler/digest

# выгрузка материалов для монтажа
curl -u operator:ПАРОЛЬ -X POST http://localhost:8000/api/export/write
```

Файлы появятся в `./exports`: CSV по раундам, дневной скоборд, полный JSON,
HTML-отчёт с графиком, SVG-график банков.

---

## 12. Если что-то не работает

| Симптом | Причина | Что делать |
|---------|---------|------------|
| `/api/doctor` → `codex_cli.available: false` | CLI не установлен или не в PATH | `codex --version`, поправьте `CODEX_CLI_COMMAND` |
| Решение Codex со статусом `FAILED` | CLI не авторизован или сменил формат вывода | `codex login`; смотрите `GET /api/stats/audit` и таблицу `api_errors` |
| Claude отвечает шаблонно | нет `ANTHROPIC_API_KEY` | `/api/doctor` покажет `claude_api_key: false` |
| В Telegram тихо | нет токена или chat_id | `/api/doctor` → `telegram: false` |
| Раундов не появляется | нет матчей в окне 20–240 мин | `GET /api/scheduler/status` покажет кандидатов |
| Раунды отменяются | Титан не успевает | увеличьте `TITAN_DEADLINE_MINUTES` или поставьте `hold` |
| Пустой список рынков | Polymarket недоступен | проверьте `curl` из шага 6 |
