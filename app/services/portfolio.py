"""Операции с банком участника и учёт движения денег."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.participants.base import PortfolioView
from app.constants import LedgerType, Outcome, PositionStatus, money
from app.db.models import LedgerEntry, Participant, Portfolio, Position
from app.schemas.snapshot import MarketSnapshot


class InsufficientFunds(RuntimeError):
    pass


def get_portfolio(db: Session, participant_id: int) -> Portfolio:
    portfolio = db.scalar(select(Portfolio).where(Portfolio.participant_id == participant_id))
    if portfolio is None:
        raise LookupError(f"портфель участника {participant_id} не найден")
    return portfolio


def open_positions(db: Session, participant_id: int, market_id: int | None = None) -> list[Position]:
    stmt = select(Position).where(
        Position.participant_id == participant_id,
        Position.status == PositionStatus.OPEN.value,
        Position.size > 0,
    )
    if market_id is not None:
        stmt = stmt.where(Position.market_id == market_id)
    return list(db.scalars(stmt))


def exposure(db: Session, participant_id: int, market_id: int | None = None) -> float:
    """Сумма вложенных денег в открытые позиции (по цене входа)."""
    return money(sum(p.cost_basis for p in open_positions(db, participant_id, market_id)))


def get_position(
    db: Session, participant_id: int, market_id: int, outcome: str
) -> Position | None:
    return db.scalar(
        select(Position).where(
            Position.participant_id == participant_id,
            Position.market_id == market_id,
            Position.outcome == outcome,
        )
    )


def equity(db: Session, participant_id: int, mark_prices: dict[tuple[int, str], float] | None = None) -> float:
    """Капитал = кэш + оценка открытых позиций (по цене входа либо по mark)."""
    portfolio = get_portfolio(db, participant_id)
    total = portfolio.cash_balance
    for pos in open_positions(db, participant_id):
        mark = None
        if mark_prices:
            mark = mark_prices.get((pos.market_id, pos.outcome))
        total += pos.size * (mark if mark is not None else pos.avg_price)
    return money(total)


def portfolio_view(
    db: Session, participant: Participant, snapshot: MarketSnapshot | None = None
) -> PortfolioView:
    portfolio = get_portfolio(db, participant.id)
    market_id = snapshot.market.market_id if snapshot else None
    return PortfolioView(
        participant_key=participant.key,
        cash_balance=money(portfolio.cash_balance),
        reserved_balance=money(portfolio.reserved_balance),
        initial_balance=money(portfolio.initial_balance),
        open_exposure_usdc=exposure(db, participant.id),
        market_exposure_usdc=exposure(db, participant.id, market_id) if market_id else 0.0,
    )


def add_ledger(
    db: Session,
    *,
    participant_id: int,
    entry_type: LedgerType,
    amount: float,
    ref_type: str | None = None,
    ref_id: int | None = None,
    note: str | None = None,
) -> LedgerEntry:
    portfolio = get_portfolio(db, participant_id)
    entry = LedgerEntry(
        participant_id=participant_id,
        entry_type=entry_type.value,
        amount=money(amount),
        cash_after=money(portfolio.cash_balance),
        equity_after=equity(db, participant_id),
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )
    db.add(entry)
    db.flush()
    return entry


def debit(db: Session, participant_id: int, amount: float, *, allow_negative: bool = False) -> None:
    portfolio = get_portfolio(db, participant_id)
    new_balance = money(portfolio.cash_balance - amount)
    if new_balance < 0 and not allow_negative:
        raise InsufficientFunds(
            f"недостаточно средств: баланс {portfolio.cash_balance}, требуется {amount}"
        )
    portfolio.cash_balance = new_balance
    db.flush()


def credit(db: Session, participant_id: int, amount: float) -> None:
    portfolio = get_portfolio(db, participant_id)
    portfolio.cash_balance = money(portfolio.cash_balance + amount)
    db.flush()


def reserve(db: Session, participant_id: int, amount: float) -> None:
    """Зарезервировать средства под заявку (деньги ещё не списаны)."""
    portfolio = get_portfolio(db, participant_id)
    if money(portfolio.available_balance - amount) < 0:
        raise InsufficientFunds(
            f"нельзя зарезервировать {amount}: доступно {portfolio.available_balance}"
        )
    portfolio.reserved_balance = money(portfolio.reserved_balance + amount)
    db.flush()


def release(db: Session, participant_id: int, amount: float) -> None:
    portfolio = get_portfolio(db, participant_id)
    portfolio.reserved_balance = money(max(0.0, portfolio.reserved_balance - amount))
    db.flush()


def opposite(outcome: str) -> str:
    return Outcome.NO.value if outcome == Outcome.YES.value else Outcome.YES.value
