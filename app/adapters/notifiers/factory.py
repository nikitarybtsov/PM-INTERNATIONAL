"""Выбор канала уведомлений."""

from __future__ import annotations

from app.adapters.notifiers.base import Notifier, NullNotifier
from app.adapters.notifiers.telegram import TelegramNotifier
from app.config import get_settings

_override: Notifier | None = None


def set_notifier_override(notifier: Notifier | None) -> None:
    """Подмена канала в тестах."""
    global _override
    _override = notifier


def get_notifier() -> Notifier:
    if _override is not None:
        return _override
    if get_settings().has_telegram():
        return TelegramNotifier()
    return NullNotifier()
