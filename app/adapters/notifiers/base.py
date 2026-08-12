"""Интерфейс уведомлений.

КЛЮЧЕВОЕ ПРАВИЛО ПРИВАТНОСТИ: канал уведомлений не должен становиться
обходным путём подсмотреть чужое решение. Сообщения до раскрытия раунда
несут только факты («Codex ответил»), но не содержимое решений.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    """Отправка текстовых уведомлений оператору."""

    name: str = "base"

    @abstractmethod
    def send(self, text: str) -> bool:
        """Отправить сообщение. Возвращает True при успехе.

        Реализация НЕ должна бросать исключение: сбой уведомления не имеет
        права ломать торговый цикл.
        """

    @property
    def enabled(self) -> bool:
        return True


class NullNotifier(Notifier):
    """Заглушка: уведомления выключены. Сообщения копятся для тестов."""

    name = "null"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True

    @property
    def enabled(self) -> bool:
        return False
