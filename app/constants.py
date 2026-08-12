"""Общие перечисления и денежные хелперы."""

from __future__ import annotations

from enum import StrEnum

# Округления: суммы USDC — 2 знака, цены — 4, размеры контрактов — 6.
MONEY_DP = 2
PRICE_DP = 4
SIZE_DP = 6


def money(value: float) -> float:
    return round(float(value) + 0.0, MONEY_DP)


def price(value: float) -> float:
    return round(float(value) + 0.0, PRICE_DP)


def size(value: float) -> float:
    return round(float(value) + 0.0, SIZE_DP)


class ParticipantKey(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    TITAN = "titan"


class ParticipantKind(StrEnum):
    AI = "AI"
    HUMAN = "HUMAN"


class Phase(StrEnum):
    """Момент, в который принимается решение.

    PREMATCH     — до начала матча, известны только составы и форма;
    AFTER_DRAFT  — драфт закончился, пики героев известны, карта ещё не началась;
    BETWEEN_MAPS — карта сыграна, оператор подтвердил счёт, следующая не началась.

    Ставок внутри идущей карты нет: это осознанное ограничение эксперимента.
    """

    PREMATCH = "PREMATCH"
    AFTER_DRAFT = "AFTER_DRAFT"
    BETWEEN_MAPS = "BETWEEN_MAPS"


class Action(StrEnum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SELL = "SELL"
    HOLD = "HOLD"


class Outcome(StrEnum):
    YES = "YES"
    NO = "NO"


class RoundStatus(StrEnum):
    """Конечный автомат раунда.

    OPEN              — snapshot зафиксирован, идёт сбор решений (скрыты)
    LOCKED            — все решения поданы, редактирование запрещено
    AWAITING_APPROVAL — risk engine отработал, оператор смотрит и одобряет
    EXECUTED          — заявки исполнены
    REVEALED          — сравнительная таблица раскрыта
    CANCELLED         — раунд отменён оператором
    """

    OPEN = "OPEN"
    LOCKED = "LOCKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTED = "EXECUTED"
    REVEALED = "REVEALED"
    CANCELLED = "CANCELLED"


class DecisionStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    FAILED = "FAILED"  # адаптер не смог получить решение (таймаут/ошибка API)


class RiskVerdict(StrEnum):
    APPROVED = "APPROVED"
    ADJUSTED = "ADJUSTED"
    REJECTED = "REJECTED"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    SETTLED = "SETTLED"


class MarketStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    SETTLED = "SETTLED"
    UNAVAILABLE = "UNAVAILABLE"


class LedgerType(StrEnum):
    INITIAL = "INITIAL"
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    TRADE_COST = "TRADE_COST"
    TRADE_PROCEEDS = "TRADE_PROCEEDS"
    FEE = "FEE"
    SETTLEMENT = "SETTLEMENT"
