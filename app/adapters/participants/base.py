"""Интерфейс участника эксперимента.

Каждый участник получает ОДИН И ТОТ ЖЕ snapshot и ОДИН И ТОТ ЖЕ набор правил.
Адаптер не имеет доступа к решениям других участников — по конструкции ему
передаётся только snapshot и состояние собственного банка.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.schemas.decision import TradeDecisionInput
from app.schemas.snapshot import MarketSnapshot


class ParticipantError(RuntimeError):
    """Ошибка получения решения (таймаут, невалидный JSON после ретраев и т.п.)."""

    def __init__(self, message: str, *, error_type: str = "adapter_error", attempts: int = 1):
        super().__init__(message)
        self.error_type = error_type
        self.attempts = attempts


@dataclass(slots=True)
class PortfolioView:
    """То, что участник знает о СВОЁМ банке. Чужие банки недоступны."""

    participant_key: str
    cash_balance: float
    reserved_balance: float
    initial_balance: float
    open_exposure_usdc: float = 0.0
    market_exposure_usdc: float = 0.0

    @property
    def available_balance(self) -> float:
        return round(self.cash_balance - self.reserved_balance, 2)


@dataclass(slots=True)
class ParticipantResult:
    decision: TradeDecisionInput
    model_name: str
    model_version: str
    prompt_version: str
    attempts: int = 1
    latency_ms: int = 0
    raw_response: str | None = None
    errors: list[dict] = field(default_factory=list)


class ParticipantAdapter(ABC):
    """Базовый адаптер участника."""

    key: str = "base"
    kind: str = "AI"

    @abstractmethod
    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> ParticipantResult:
        """Вернуть решение по snapshot'у. Синхронный вызов с таймаутом внутри."""

    @property
    def is_mock(self) -> bool:
        return False
