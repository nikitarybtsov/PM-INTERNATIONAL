"""Расчёт после определения результата рынка.

Результат вводится оператором вручную (`winning_outcome` = YES | NO).
Победивший исход гасится по 1.00 USDC за контракт, проигравший — по 0.00.

Отдельный случай — SPLIT: событие не состоялось, и Polymarket гасит ОБЕ
стороны по 0.50. Так закрываются рынки третьей карты, если серия закончилась
2:0. Деньги при этом не возвращаются по цене покупки: контракт, купленный по
0.72, принесёт 0.50 — минус 22 цента, а купленный по 0.24 даст плюс 26.
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
    if winning_outcome not in ("YES", "NO", "SPLIT"):
        raise ValueError("winning_outcome должен быть YES, NO или SPLIT")

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
        # SPLIT — событие не состоялось: обе стороны по 0.50 за контракт.
        if winning_outcome == "SPLIT":
            unit_payout = 0.5
        else:
            unit_payout = 1.0 if pos.outcome == winning_outcome else 0.0
        payout = money(pos.size * unit_payout)
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


def auto_settle(db: Session, *, actor: str = "auto") -> dict:
    """Забрать результаты рынков с биржи и рассчитать всё, что уже разрешено.

    Оператору не нужно вводить исходы руками: Polymarket сам разрешает рынки
    любого типа — победителя серии, тотал, фору, экзотику. Мы лишь спрашиваем
    итог и гасим позиции.

    Рынки, которые биржа ещё не разрешила (идёт спор через UMA или матч не
    закончен), пропускаются — вернёмся к ним на следующем проходе.
    """
    from app.adapters.market_data import MarketNotAvailable
    from app.services.snapshots import provider_for

    provider = provider_for(db)
    settled_ids = {s.market_id for s in db.scalars(select(Settlement))}

    # Интересуют только рынки, где у кого-то есть открытая позиция: гонять
    # запросы по всему каталогу незачем.
    market_ids = {
        row.market_id
        for row in db.scalars(
            select(Position).where(Position.status == PositionStatus.OPEN.value)
        )
    }

    report = {"checked": 0, "settled": [], "pending": [], "errors": []}
    for market_id in sorted(market_ids - settled_ids):
        market = db.get(Market, market_id)
        if market is None:
            continue
        report["checked"] += 1
        try:
            outcome = provider.get_resolution(market.external_id)
        except (MarketNotAvailable, Exception) as exc:  # noqa: BLE001
            report["errors"].append({"market_id": market_id, "error": str(exc)[:200]})
            continue

        if outcome is None:
            report["pending"].append({"market_id": market_id, "title": market.title})
            continue

        settle_market(
            db, market, outcome, actor=actor,
            note="результат получен от Polymarket автоматически",
        )
        report["settled"].append(
            {"market_id": market_id, "title": market.title, "outcome": outcome}
        )
    return report
