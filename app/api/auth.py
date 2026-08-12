"""Защита панели.

Две независимые двери:

  * оператор — HTTP Basic (PANEL_USER / PANEL_PASSWORD) на всё, кроме /health;
  * Титан — страница `/ui/rounds/{id}/titan` и отправка его решения доступны
    по одноразовой ссылке с токеном (TITAN_ACCESS_TOKEN), чтобы человеку не
    нужно было знать операторский пароль.

Если PANEL_PASSWORD не задан, авторизация выключена — так работает локальный
запуск. При публикации наружу пароль обязателен, о чём предупреждает /health.
"""

from __future__ import annotations

import secrets

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.config import get_settings

PUBLIC_PATHS = ("/health", "/static", "/favicon.ico")
TITAN_PATH_MARKER = "/titan"


def _unauthorized() -> JSONResponse:
    """Ответ, а не исключение.

    Middleware работает выше обработчиков исключений FastAPI: поднятый здесь
    HTTPException не превратится в 401, а вылетит наружу как ошибка сервера.
    """
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "требуется авторизация"},
        headers={"WWW-Authenticate": 'Basic realm="PM-INTERNATIONAL"'},
    )


def _check_basic(request: Request) -> bool:
    settings = get_settings()
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return False
    import base64
    import binascii

    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, _, password = raw.partition(":")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user, settings.panel_user) and secrets.compare_digest(
        password, settings.panel_password or ""
    )


def _check_titan_token(request: Request) -> bool:
    settings = get_settings()
    expected = settings.titan_access_token
    if not expected:
        return False
    supplied = request.query_params.get("t") or request.headers.get("X-Titan-Token", "")
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def is_titan_route(path: str, method: str) -> bool:
    """Страница Титана и отправка его решения."""
    if path.endswith(TITAN_PATH_MARKER):
        return True
    return method == "POST" and path.endswith("/decisions/titan")


async def auth_middleware(request: Request, call_next):
    settings = get_settings()
    path = request.url.path

    if not settings.panel_auth_enabled() or path.startswith(PUBLIC_PATHS):
        return await call_next(request)

    if is_titan_route(path, request.method) and _check_titan_token(request):
        return await call_next(request)

    if _check_basic(request):
        return await call_next(request)

    return _unauthorized()
