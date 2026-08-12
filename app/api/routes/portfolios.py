"""Портфели, позиции, журнал операций."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import PositionStatus
from app.db.base import get_db
from app.db.models import LedgerEntry, Market, Participant, Position, SimulatedOrder
from app.services import paper_engine
from app.services import portfolio as pf

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])


@router.get("/titan/live-trades")
def titan_live_trades() -> dict:
    """Сделки Титана прямо с его кошелька на Polymarket.

    Титан торгует руками со своего аккаунта — система ничего за него не решает
    и не исполняет. Нужен только публичный адрес: приватный ключ для чтения
    сделок не требуется и намеренно не поддерживается.
    """
    import httpx

    from app.config import get_settings

    address = (get_settings().titan_poly_address or "").strip()
    if not address:
        return {"address": None, "trades": [], "note": "TITAN_POLY_ADDRESS не задан"}

    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.get(
                "https://data-api.polymarket.com/trades",
                params={"user": address, "limit": 50},
            )
            response.raise_for_status()
            raw = response.json()
    except Exception as exc:  # noqa: BLE001 — биржа недоступна, это не отказ панели
        raise HTTPException(
            status_code=502, detail=f"не удалось получить сделки: {exc}"
        ) from exc

    trades = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        trades.append(
            {
                "title": item.get("title") or item.get("slug"),
                "outcome": item.get("outcome"),
                "side": item.get("side"),
                "size": float(item.get("size") or 0),
                "price": float(item.get("price") or 0),
                "timestamp": item.get("timestamp"),
                "transaction_hash": item.get("transactionHash"),
            }
        )
    return {"address": address, "trades": trades}


@router.get("")
def list_portfolios(db: Session = Depends(get_db)) -> list[dict]:
    marks = paper_engine.latest_marks(db)
    out = []
    for participant in db.scalars(select(Participant).order_by(Participant.id)):
        portfolio = pf.get_portfolio(db, participant.id)
        open_positions = pf.open_positions(db, participant.id)
        open_value = round(
            sum(p.size * marks.get((p.market_id, p.outcome), p.avg_price) for p in open_positions), 2
        )
        out.append(
            {
                "participant": participant.key,
                "display_name": participant.display_name,
                "kind": participant.kind,
                "initial_balance": portfolio.initial_balance,
                "cash_balance": portfolio.cash_balance,
                "reserved_balance": portfolio.reserved_balance,
                "available_balance": portfolio.available_balance,
                "open_positions_value": open_value,
                "equity": round(portfolio.cash_balance + open_value, 2),
                "open_positions_count": len(open_positions),
            }
        )
    return out


@router.get("/{participant_key}/positions")
def positions(participant_key: str, db: Session = Depends(get_db)) -> list[dict]:
    participant = db.scalar(select(Participant).where(Participant.key == participant_key))
    if participant is None:
        raise HTTPException(status_code=404, detail="участник не найден")
    marks = paper_engine.latest_marks(db)
    out = []
    for pos in db.scalars(
        select(Position).where(Position.participant_id == participant.id).order_by(Position.id)
    ):
        market = db.get(Market, pos.market_id)
        mark = marks.get((pos.market_id, pos.outcome), pos.avg_price)
        unrealized = (
            round(pos.size * mark - pos.cost_basis, 2)
            if pos.status == PositionStatus.OPEN.value
            else 0.0
        )
        out.append(
            {
                "id": pos.id,
                "market_id": pos.market_id,
                "market": market.title if market else None,
                "outcome": pos.outcome,
                "size": pos.size,
                "avg_price": pos.avg_price,
                "cost_basis": pos.cost_basis,
                "mark_price": mark,
                "unrealized_pnl": unrealized,
                "realized_pnl": pos.realized_pnl,
                "status": pos.status,
                "phase_opened": pos.phase_opened,
            }
        )
    return out


@router.get("/{participant_key}/orders")
def orders(participant_key: str, db: Session = Depends(get_db)) -> list[dict]:
    participant = db.scalar(select(Participant).where(Participant.key == participant_key))
    if participant is None:
        raise HTTPException(status_code=404, detail="участник не найден")
    out = []
    for order in db.scalars(
        select(SimulatedOrder)
        .where(SimulatedOrder.participant_id == participant.id)
        .order_by(SimulatedOrder.id.desc())
    ):
        market = db.get(Market, order.market_id)
        out.append(
            {
                "id": order.id,
                "round_id": order.round_id,
                "market": market.title if market else None,
                "action": order.action,
                "outcome": order.outcome,
                "status": order.status,
                "filled_size": order.filled_size,
                "avg_fill_price": order.avg_fill_price,
                "notional": order.notional,
                "fee": order.fee,
                "slippage_bps": order.slippage_bps,
                "reject_reason": order.reject_reason,
                "created_at": order.created_at,
            }
        )
    return out


@router.get("/{participant_key}/ledger")
def ledger(participant_key: str, db: Session = Depends(get_db)) -> list[dict]:
    participant = db.scalar(select(Participant).where(Participant.key == participant_key))
    if participant is None:
        raise HTTPException(status_code=404, detail="участник не найден")
    return [
        {
            "id": e.id,
            "type": e.entry_type,
            "amount": e.amount,
            "cash_after": e.cash_after,
            "equity_after": e.equity_after,
            "note": e.note,
            "created_at": e.created_at,
        }
        for e in db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.participant_id == participant.id)
            .order_by(LedgerEntry.id)
        )
    ]
