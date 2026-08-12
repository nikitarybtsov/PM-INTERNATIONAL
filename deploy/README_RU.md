# Деплой на сервер

Разворачивание на Linux с systemd. Режим остаётся **paper trading**: реальных
сделок нет и включить их конфигурацией нельзя.

## Что где лежит

| | |
|---|---|
| Каталог на сервере | `/opt/pm-international` |
| Сервис | `pm-international.service` |
| Панель | `http://127.0.0.1:8000` — **только localhost** |
| База | `/opt/pm-international/data/pm.db` (SQLite) |
| Конфигурация | `/opt/pm-international/.env` (правится на сервере) |
| Журнал | `journalctl -u pm-international -f` |

## Разовая подготовка сервера

Нужны `python3.12`, `python3.12-venv`, `git`, `curl`, `node`/`npm` (для CLI
участников).

```bash
apt update && apt install -y python3.12 python3.12-venv curl git
```

Участники работают через залогиненные CLI — API-ключи не нужны:

```bash
npm install -g @openai/codex @anthropic-ai/claude-code
codex login      # интерактивно, один раз, вашим аккаунтом ChatGPT
claude           # интерактивно, один раз; затем /login внутри
```

Вход обязательно выполнять **под тем же пользователем**, от которого работает
сервис (в юните это root, `HOME=/root`). Иначе CLI не найдёт авторизацию и
участник будет отклоняться в каждом раунде.

## Выкатка

На рабочей машине в `.env` (он в `.gitignore`):

```bash
DEPLOY_HOST=194.163.136.178
DEPLOY_USER=root
SERVER_PASSWORD=<пароль>
```

Затем:

```bash
pip install -r requirements-deploy.txt
python deploy/deploy.py
```

Скрипт заливает код, создаёт venv, ставит зависимости, применяет миграции,
устанавливает юнит и перезапускает сервис. Серверный `.env` **не
перезаписывается**: при первом деплое он создаётся из `.env.example`, дальше его
правит оператор. Локальный `.env` на сервер не копируется — в нём пароль от
самого сервера.

Остальные команды:

```bash
python deploy/deploy.py --status    # состояние сервиса и /health
python deploy/deploy.py --doctor    # готовность участников и источника данных
python deploy/deploy.py --logs      # последние строки журнала
python deploy/deploy.py --restart   # перезапуск без выкатки
```

## Боевая конфигурация

В `/opt/pm-international/.env`:

```bash
MARKET_DATA_PROVIDER=polymarket
POLYMARKET_SEARCH_QUERY=The International

CODEX_TRANSPORT=cli
CLAUDE_TRANSPORT=cli
```

После правки — `systemctl restart pm-international`.

Проверка готовности:

```bash
curl -s localhost:8000/api/doctor
```

У обоих участников должно быть `available: true`. Пока это не так, участник
работает заглушкой (при `api` без ключа) или отклоняется в каждом раунде
(при `cli` без установленного и авторизованного бинаря).

## Доступ снаружи

Панель слушает только `127.0.0.1` — это осознанно: без `PANEL_PASSWORD` её
откроет любой, кто дотянется до порта. Варианты доступа:

**SSH-проброс для себя** (ничего настраивать не нужно):

```bash
ssh -N -L 8000:127.0.0.1:8000 root@<сервер>
# панель на http://localhost:8000 у вас на машине
```

**Cloudflare Tunnel для Титана** — см. RUNBOOK, раздел 9. Перед этим
обязательно задайте в серверном `.env`:

```bash
PANEL_PASSWORD=<длинный пароль>
TITAN_ACCESS_TOKEN=<токен>          # python -c "import secrets; print(secrets.token_urlsafe(24))"
PUBLIC_BASE_URL=https://<ваш-домен>
```

Титан открывает `/ui/rounds/{id}/titan?t=<токен>` — только форму и свой банк,
без операторских страниц и без решений других участников.

## Обновление

```bash
python deploy/deploy.py
```

Данные (`data/`, `exports/`) и серверный `.env` не затрагиваются. Схема БД
обновляется миграциями Alembic.

## Откат и остановка

```bash
systemctl stop pm-international
systemctl disable pm-international     # чтобы не поднимался после перезагрузки
```

Полное удаление:

```bash
systemctl stop pm-international && systemctl disable pm-international
rm /etc/systemd/system/pm-international.service && systemctl daemon-reload
rm -rf /opt/pm-international           # вместе с базой и экспортами
```

## Сброс эксперимента

Начать с чистых банков:

```bash
systemctl stop pm-international
rm -f /opt/pm-international/data/pm.db
cd /opt/pm-international && .venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.cli init
systemctl start pm-international
```
