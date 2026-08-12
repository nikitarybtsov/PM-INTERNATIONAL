"""SSH-туннель к панели сервера.

    python deploy/tunnel.py            # localhost:8000 → сервер:8000
    python deploy/tunnel.py --port 8080

Панель на сервере слушает только localhost — снаружи её нет намеренно, потому
что без PANEL_PASSWORD её открыл бы любой. Туннель даёт доступ только вам.

Реализован на paramiko, а не на системном ssh: доступ берётся из того же .env,
что и деплой, пароль вводить не нужно, и работает одинаково на Windows и Linux.

Окно не закрывать: туннель живёт, пока идёт эта команда.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import socketserver
import sys
import threading
import time
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.exit("Нужен paramiko: pip install -r requirements-deploy.txt")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

LOCAL_DIR = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(LOCAL_DIR / ".env")

REMOTE_HOST = "127.0.0.1"
REMOTE_PORT = 8000


class _Handler(socketserver.BaseRequestHandler):
    ssh_transport: paramiko.Transport
    chain_host: str
    chain_port: int

    def handle(self) -> None:
        try:
            channel = self.ssh_transport.open_channel(
                "direct-tcpip",
                (self.chain_host, self.chain_port),
                self.request.getpeername(),
            )
        except Exception as exc:  # noqa: BLE001 — соединение просто не состоится
            print(f"  канал не открылся: {exc}")
            return
        if channel is None:
            return

        try:
            while True:
                ready, _, _ = select.select([self.request, channel], [], [])
                if self.request in ready:
                    data = self.request.recv(16384)
                    if not data:
                        break
                    channel.send(data)
                if channel in ready:
                    data = channel.recv(16384)
                    if not data:
                        break
                    self.request.send(data)
        except OSError:
            pass
        finally:
            channel.close()
            self.request.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Туннель к панели сервера")
    parser.add_argument("--port", type=int, default=8000, help="локальный порт")
    parser.add_argument("--open", action="store_true", help="открыть браузер")
    args = parser.parse_args()

    host = (os.getenv("DEPLOY_HOST") or "").strip()
    user = (os.getenv("DEPLOY_USER") or "root").strip()
    password = (os.getenv("SERVER_PASSWORD") or "").strip()
    if not host or not password:
        sys.exit("Нужны DEPLOY_HOST и SERVER_PASSWORD в .env")

    port = int(os.getenv("DEPLOY_PORT") or "22")
    url = f"http://localhost:{args.port}/"

    def connect() -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host, port=port, username=user, password=password,
            timeout=25, allow_agent=False, look_for_keys=False,
        )
        return client

    client = connect()
    print(f"Подключено: {user}@{host}")
    print(f"Панель: {url}")
    print("Окно не закрывайте — туннель живёт, пока идёт эта команда (Ctrl+C — выход).")
    if args.open:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    server: _Server | None = None
    try:
        while True:
            handler = type(
                "Handler",
                (_Handler,),
                {
                    "ssh_transport": client.get_transport(),
                    "chain_host": REMOTE_HOST,
                    "chain_port": REMOTE_PORT,
                },
            )
            server = _Server(("127.0.0.1", args.port), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            # Деплой перезапускает сервис и рвёт соединение. Без переподключения
            # туннель молча умирал бы, и панель переставала открываться.
            while True:
                time.sleep(5)
                transport = client.get_transport()
                if transport is None or not transport.is_active():
                    print("Соединение потеряно, переподключаюсь…")
                    break

            server.shutdown()
            server.server_close()
            server = None
            with contextlib.suppress(Exception):
                client.close()

            for attempt in range(1, 61):
                try:
                    client = connect()
                    print("Соединение восстановлено.")
                    break
                except Exception as exc:  # noqa: BLE001 — ждём, пока сервер вернётся
                    if attempt == 1:
                        print(f"  сервер недоступен ({type(exc).__name__}), жду…")
                    time.sleep(5)
            else:
                print("Не удалось переподключиться за 5 минут. Выхожу.")
                return 1
    except KeyboardInterrupt:
        print("\nТуннель закрыт.")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        with contextlib.suppress(Exception):
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
