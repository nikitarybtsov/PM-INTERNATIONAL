"""Интерфейс исполнения ордеров.

Отделён от бумажного движка намеренно: `services/paper_engine.py` остаётся
источником истины для статистики и не умеет ходить в сеть, а сюда вынесено
всё, что связано с реальной биржей.

Правила, общие для любой реализации:

* исполнение всегда **тейкерское** — мы выкупаем стакан, а не встаём в него;
* ни одна реализация не отправляет ордер без явного одобрения оператора:
  флаг `approved_by` обязателен и проверяется до обращения к сети;
* при `dry_run` ордер только рассчитывается и логируется.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class ExecutionError(RuntimeError):
    """Ордер не удалось отправить или биржа его отклонила."""


class NotApproved(ExecutionError):
    """Попытка исполнить сделку без одобрения оператора."""


class OrderStatus(str, Enum):
    DRY_RUN = "DRY_RUN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(slots=True)
class OrderRequest:
    """Заявка на покупку исхода тейкером."""

    participant: str
    market_id: int
    token_id: str
    outcome: str
    #: Сколько контрактов купить. Стоимость ≈ size × max_price.
    size: float
    #: Потолок цены: выше него исполнять нельзя.
    max_price: float
    #: Кто одобрил сделку. Без этого исполнение запрещено.
    approved_by: str | None = None
    #: Идемпотентность: повторная отправка с тем же ключом должна отсекаться.
    idempotency_key: str | None = None

    @property
    def notional_usdc(self) -> float:
        return round(self.size * self.max_price, 6)


@dataclass(slots=True)
class OrderResult:
    """Результат отправки: что реально исполнилось."""

    status: OrderStatus
    filled_size: float = 0.0
    avg_price: float = 0.0
    fee_usdc: float = 0.0
    order_id: str | None = None
    error: str | None = None
    dry_run: bool = False
    raw: dict = field(default_factory=dict)
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def notional_usdc(self) -> float:
        return round(self.filled_size * self.avg_price, 6)

    @property
    def is_success(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.DRY_RUN)


class ExecutionAdapter(ABC):
    """Отправка заявок на биржу."""

    name: str = "base"

    @abstractmethod
    def execute(self, request: OrderRequest) -> OrderResult:
        """Исполнить заявку. Обязан отклонить её без `approved_by`."""

    @abstractmethod
    def balance_usdc(self, participant: str) -> float | None:
        """Баланс кошелька участника или None, если недоступен."""

    def close(self) -> None:  # pragma: no cover - по умолчанию нечего закрывать
        return None
