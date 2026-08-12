"""Расчёт после определения результата рынка.

Результат вводится оператором вручную (`winning_outcome` = YES | NO).
Победивший исход гасится по 1.00 USDC за контракт, проигравший — по 0.00.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import LedgerType, MarketStatus, PositionStatus, money
from app.db.models import Market, Position, Settlement
from app.services import audit
from app.services import portfolio as pf


class AlreadySettled(RuntimeError):
    pass


def settle_market(
    db: Session,
    market: Market,
    winning_outcome: str,
    *,
    actor: str = "operator",
    note: str | None = None,
) -> Settlement:
    if winning_outcome not in ("YES", "NO"):
        raise ValueError("winning_outcome должен быть YES или NO")

    existing = db.scalar(select(Settlement).where(Settlement.market_id == market.id))
    if existing is not None:
        raise AlreadySettled(
            f"рынок {market.external_id} уже рассчитан: победил {existing.winning_outcome}"
        )

    settlement = Settlement(
        market_id=market.id, winning_outcome=winning_outcome, source="manual", note=note
    )
    db.add(settlement)
    db.flush()

    positions = list(
        db.scalars(
            select(Position).where(
                Position.market_id == market.id,
                Position.status == PositionStatus.OPEN.value,
            )
        )
    )
    for pos in positions:
        payout = money(pos.size * (1.0 if pos.outcome == winning_outcome else 0.0))
        realized = money(payout - pos.cost_basis)
        pos.realized_pnl = money(pos.realized_pnl + realized)
        pos.status = PositionStatus.SETTLED.value
        pos.size = 0.0
        pos.cost_basis = 0.0
        db.flush()

        if payout:
            pf.credit(db, pos.participant_id, payout)
        pf.add_ledger(
            db,
            participant_id=pos.participant_id,
            entry_type=LedgerType.SETTLEMENT,
            amount=payout,
            ref_type="settlement",
            ref_id=settlement.id,
            note=(
                f"расчёт {market.external_id}: победил {winning_outcome}, "
                f"позиция {pos.outcome}, выплата {payout:.2f}, PnL {realized:+.2f}"
            ),
        )

    market.status = MarketStatus.SETTLED.value
    db.flush()

    audit.record(
        db,
        entity_type="market",
        entity_id=market.id,
        action="settle",
        actor=actor,
        after={
            "winning_outcome": winning_outcome,
            "positions_settled": len(positions),
        },
        note=note,
    )
    return settlement
