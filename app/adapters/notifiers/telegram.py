"""Отправка уведомлений в Telegram.

Токен читается только из окружения (TELEGRAM_BOT_TOKEN) и никогда не
попадает в БД, логи и экспорты — он маскируется в `audit.scrub`.

Сбой отправки логируется и возвращает False, но не прерывает раунд.
"""

from __future__ import annotations

import logging

import httpx

from app.adapters.notifiers.base import Notifier
from app.config import get_settings

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._token = token or settings.telegram_bot_token
        self._chat_id = chat_id or settings.telegram_chat_id
        self._timeout = settings.telegram_timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def _post(self, chunk: str) -> bool:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(url, json=payload)
            if response.status_code >= 400:
                # тело ответа Telegram не содержит токена, но обрезаем на всякий случай
                logger.warning(
                    "Telegram отклонил сообщение: HTTP %s %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("не удалось отправить в Telegram: %s", type(exc).__name__)
            return False
        finally:
            if self._owns_client:
                client.close()

    def send(self, text: str) -> bool:
        if not self.enabled:
            logger.debug("Telegram не настроен — сообщение пропущено")
            return False
        ok = True
        for chunk in _split(text):
            ok = self._post(chunk) and ok
        return ok


def _split(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Разбить длинное сообщение по границам строк."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks
