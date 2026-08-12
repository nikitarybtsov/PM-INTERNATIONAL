"""Разворачивает PM-INTERNATIONAL на сервер по SSH и поднимает под systemd.

    python deploy/deploy.py            # выкатить код и перезапустить сервис
    python deploy/deploy.py --status   # состояние сервиса и health
    python deploy/deploy.py --logs     # последние строки журнала
    python deploy/deploy.py --doctor   # проверка готовности участников
    python deploy/deploy.py --restart  # только перезапуск

Доступ берётся из переменных окружения (или .env, который в .gitignore):
DEPLOY_HOST, DEPLOY_USER, SERVER_PASSWORD, опционально DEPLOY_PORT.
Пароль нигде не сохраняется и не печатается.

Серверный .env НЕ перезаписывается: при первом деплое он создаётся из
.env.example, дальше его правит оператор. Локальный .env на сервер не уезжает —
в нём лежит пароль от самого сервера.

Скрипт не включает и не может включить реальную торговлю: live execution в
проекте физически отсутствует.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - подсказка вместо стектрейса
    sys.exit("Нужен paramiko: pip install -r requirements-deploy.txt")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

LOCAL_DIR = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(LOCAL_DIR / ".env")

REMOTE_DIR = "/opt/pm-international"
SERVICE = "pm-international"
UNIT_SRC = LOCAL_DIR / "deploy" / f"{SERVICE}.service"

# Что копируем. Всё остальное (venv, БД, экспорты, .git) живёт только на сервере.
PAYLOAD_DIRS = ["app", "alembic", "tests"]
PAYLOAD_FILES = [
    "alembic.ini",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    ".env.example",
    "README.md",
    "RUNBOOK.md",
    "CHECKLIST.md",
]
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".idea"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".db", ".sqlite3")


def connect() -> paramiko.SSHClient:
    host = (os.getenv("DEPLOY_HOST") or "").strip()
    user = (os.getenv("DEPLOY_USER") or "root").strip()
    password = (os.getenv("SERVER_PASSWORD") or "").strip()
    port = int(os.getenv("DEPLOY_PORT") or "22")
    if not host or not password:
        sys.exit(
            "Нужны DEPLOY_HOST и SERVER_PASSWORD в окружении или .env.\n"
            "Пример:\n  DEPLOY_HOST=194.163.136.178\n  DEPLOY_USER=root\n"
            "  SERVER_PASSWORD=<пароль>"
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, port=port, username=user, password=password,
        timeout=25, allow_agent=False, look_for_keys=False,
    )
    print(f"Подключено: {user}@{host}")
    return client


def run(client: paramiko.SSHClient, command: str, check: bool = True, timeout: int = 900) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        sys.exit(f"Команда упала ({code}): {command}\n{(err or out).strip()[:2000]}")
    return (out + err).strip()


def _iter_files() -> list[tuple[Path, str]]:
    """Пары (локальный путь, относительный путь) для заливки."""
    items: list[tuple[Path, str]] = []
    for name in PAYLOAD_FILES:
        path = LOCAL_DIR / name
        if path.exists():
            items.append((path, name))
    for folder in PAYLOAD_DIRS:
        root = LOCAL_DIR / folder
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            items.append((path, path.relative_to(LOCAL_DIR).as_posix()))
    return items


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str, made: set[str]) -> None:
    if remote_dir in made or remote_dir in ("", "/"):
        return
    parent = posixpath.dirname(remote_dir)
    _ensure_remote_dir(sftp, parent, made)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        sftp.mkdir(remote_dir)
    made.add(remote_dir)


def upload(client: paramiko.SSHClient) -> int:
    sftp = client.open_sftp()
    made: set[str] = set()
    files = _iter_files()
    try:
        _ensure_remote_dir(sftp, REMOTE_DIR, made)
        for local_path, rel in files:
            remote_path = posixpath.join(REMOTE_DIR, rel)
            _ensure_remote_dir(sftp, posixpath.dirname(remote_path), made)
            sftp.put(str(local_path), remote_path)
        # каталоги под БД и экспорты — на сервере, в payload их нет
        for extra in ("data", "exports"):
            _ensure_remote_dir(sftp, posixpath.join(REMOTE_DIR, extra), made)
        # unit-файл кладём отдельно
        _ensure_remote_dir(sftp, posixpath.join(REMOTE_DIR, "deploy"), made)
        sftp.put(str(UNIT_SRC), posixpath.join(REMOTE_DIR, "deploy", UNIT_SRC.name))
    finally:
        sftp.close()
    print(f"Залито файлов: {len(files)}")
    return len(files)


def ensure_env(client: paramiko.SSHClient) -> None:
    """Создать серверный .env при первом деплое. Существующий не трогаем."""
    exists = run(
        client, f"test -f {REMOTE_DIR}/.env && echo yes || echo no", check=False
    ).endswith("yes")
    if exists:
        print(".env на сервере уже есть — оставлен без изменений")
        return
    run(client, f"cp {REMOTE_DIR}/.env.example {REMOTE_DIR}/.env")
    run(client, f"chmod 600 {REMOTE_DIR}/.env")
    print(".env создан из .env.example — проверьте настройки перед боевым запуском")


def ensure_venv(client: paramiko.SSHClient) -> None:
    run(client, f"test -x {REMOTE_DIR}/.venv/bin/python || python3.12 -m venv {REMOTE_DIR}/.venv")
    run(client, f"{REMOTE_DIR}/.venv/bin/pip install -q --upgrade pip")
    run(client, f"{REMOTE_DIR}/.venv/bin/pip install -q -r {REMOTE_DIR}/requirements.txt")
    version = run(client, f"{REMOTE_DIR}/.venv/bin/python --version")
    print(f"Окружение готово: {version}")


def install_unit(client: paramiko.SSHClient) -> None:
    run(client, f"cp {REMOTE_DIR}/deploy/{SERVICE}.service /etc/systemd/system/{SERVICE}.service")
    run(client, "systemctl daemon-reload")
    run(client, f"systemctl enable {SERVICE} >/dev/null 2>&1", check=False)
    print(f"Юнит установлен: /etc/systemd/system/{SERVICE}.service")


def migrate(client: paramiko.SSHClient) -> None:
    out = run(
        client,
        f"cd {REMOTE_DIR} && .venv/bin/python -m alembic upgrade head 2>&1 | tail -3",
        check=False,
    )
    print(f"Миграции: {out or 'без вывода'}")
    run(client, f"cd {REMOTE_DIR} && .venv/bin/python -m app.cli init", check=False)


def restart(client: paramiko.SSHClient) -> None:
    run(client, f"systemctl restart {SERVICE}")
    print(f"Сервис перезапущен: {SERVICE}")


def status(client: paramiko.SSHClient) -> None:
    print(run(client, f"systemctl status {SERVICE} --no-pager | head -12", check=False))
    print("\n--- /health ---")
    print(run(client, "curl -s -m 10 http://127.0.0.1:8000/health", check=False) or "(нет ответа)")


def doctor(client: paramiko.SSHClient) -> None:
    print(run(client, "curl -s -m 30 http://127.0.0.1:8000/api/doctor", check=False)
          or "(нет ответа)")


def logs(client: paramiko.SSHClient) -> None:
    print(run(client, f"journalctl -u {SERVICE} -n 40 --no-pager", check=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Деплой PM-INTERNATIONAL")
    parser.add_argument("--status", action="store_true", help="состояние сервиса")
    parser.add_argument("--logs", action="store_true", help="журнал сервиса")
    parser.add_argument("--doctor", action="store_true", help="проверка готовности участников")
    parser.add_argument("--restart", action="store_true", help="только перезапуск")
    parser.add_argument("--no-restart", action="store_true", help="выкатить без перезапуска")
    args = parser.parse_args()

    client = connect()
    try:
        if args.status:
            status(client)
        elif args.logs:
            logs(client)
        elif args.doctor:
            doctor(client)
        elif args.restart:
            restart(client)
            status(client)
        else:
            upload(client)
            ensure_env(client)
            ensure_venv(client)
            install_unit(client)
            migrate(client)
            if not args.no_restart:
                restart(client)
                status(client)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
