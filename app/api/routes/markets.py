"""Рынки: поиск у провайдера, список, заметки оператора, расчёт результата."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data import MarketNotAvailable
from app.api.schemas import OperatorNotesRequest, SeedRequest, SettleRequest
from app.db.base import get_db
from app.db.models import Market, Settlement
from app.services import audit, notifications
from app.services import seed as seed_service
from app.services import settlement as settlement_service
from app.services.snapshots import provider_for, upsert_market

router = APIRouter(prefix="/api/markets", tags=["markets"])


def _market_dto(db: Session, market: Market) -> dict:
    settled = db.scalar(select(Settlement).where(Settlement.market_id == market.id))
    return {
        "id": market.id,
        "source": market.source,
        "external_id": market.external_id,
        "title": market.title,
        "event_title": market.event_title,
        "tournament": market.tournament,
        "market_type": market.market_type,
        "team_a": market.team_a,
        "team_b": market.team_b,
        "yes_label": market.yes_label,
        "no_label": market.no_label,
        "starts_at": market.starts_at,
        "status": market.status,
        "operator_notes": market.operator_notes,
        "settled_outcome": settled.winning_outcome if settled else None,
    }


@router.get("")
def list_markets(db: Session = Depends(get_db)) -> list[dict]:
    return [_market_dto(db, m) for m in db.scalars(select(Market).order_by(Market.id))]


@router.post("/refresh")
def refresh_markets(payload: SeedRequest, db: Session = Depends(get_db)) -> dict:
    """Подтянуть рынки Dota 2 / The International у активного провайдера."""
    try:
        markets = seed_service.seed_markets(db, query=payload.query, limit=payload.limit)
    except MarketNotAvailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"count": len(markets), "markets": [_market_dto(db, m) for m in markets]}


@router.get("/search")
def search_markets(q: str = "Dota", limit: int = 25, db: Session = Depends(get_db)) -> list[dict]:
    provider = provider_for(db)
    try:
        refs = provider.search_markets(q, limit=limit)
    except MarketNotAvailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [
        {
            "external_id": r.external_id,
            "title": r.title,
            "market_type": r.market_type,
            "team_a": r.team_a,
            "team_b": r.team_b,
            "starts_at": r.starts_at,
            "source": r.source,
        }
        for r in refs
    ]


@router.post("/import/{external_id}")
def import_market(external_id: str, db: Session = Depends(get_db)) -> dict:
    provider = provider_for(db)
    try:
        ref = provider.get_market(external_id)
    except MarketNotAvailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _market_dto(db, upsert_market(db, ref))


@router.get("/{market_id}")
def get_market(market_id: int, db: Session = Depends(get_db)) -> dict:
    market = db.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="рынок не найден")
    return _market_dto(db, market)


@router.post("/{market_id}/notes")
def set_notes(
    market_id: int, payload: OperatorNotesRequest, db: Session = Depends(get_db)
) -> dict:
    """Заметки оператора (составы, новости) попадают в следующий snapshot."""
    market = db.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="рынок не найден")
    before = market.operator_notes
    market.operator_notes = payload.notes
    db.flush()
    audit.record(
        db,
        entity_type="market",
        entity_id=market.id,
        action="operator_notes",
        actor="operator",
        before={"operator_notes": before},
        after={"operator_notes": payload.notes},
    )
    return _market_dto(db, market)


@router.post("/settle-auto")
def settle_auto(db: Session = Depends(get_db)) -> dict:
    """Забрать результаты с Polymarket и рассчитать всё, что уже разрешено.

    Работает для любых рынков — победитель серии, тотал, фора, экзотика.
    Ручной ввод результата остаётся как запасной путь, если биржа затянет
    с разрешением или потребуется вмешательство.
    """
    report = settlement_service.auto_settle(db)
    for item in report["settled"]:
        market = db.get(Market, item["market_id"])
        if market is not None:
            notifications.market_settled(db, market, item["outcome"])
    return report


@router.post("/{market_id}/settle")
def settle(market_id: int, payload: SettleRequest, db: Session = Depends(get_db)) -> dict:
    """Оператор вручную выставляет результат рынка; банки пересчитываются.

    Обычно не нужен: результаты приезжают сами через `/settle-auto`. Оставлен
    на случай, когда биржа затягивает с разрешением, а расчёт нужен сейчас.
    """
    market = db.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="рынок не найден")
    try:
        row = settlement_service.settle_market(
            db, market, payload.winning_outcome, note=payload.note
        )
    except settlement_service.AlreadySettled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notifications.market_settled(db, market, row.winning_outcome)
    return {"market_id": market.id, "winning_outcome": row.winning_outcome, "settled": True}


@router.post("/seed")
def seed(payload: SeedRequest, db: Session = Depends(get_db)) -> dict:
    return seed_service.seed_all(db, with_markets=payload.with_markets)


@router.get("/{market_id}/draft")
def live_draft(market_id: int, db: Session = Depends(get_db)) -> dict:
    """Пики текущей карты из живой трансляции.

    Оператору не нужно набирать составы руками: Valve отдаёт их с задержкой
    около 10 секунд после драфта.
    """
    from app.services import draft as draft_service

    market = db.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="рынок не найден")
    if not market.team_a or not market.team_b:
        raise HTTPException(status_code=422, detail="у рынка не заданы команды")

    found = draft_service.fetch_draft(market.team_a, market.team_b)
    if found is None:
        return {
            "found": False,
            "message": (
                f"матч {market.team_a} — {market.team_b} не найден в эфире "
                f"или драфт ещё не закончен"
            ),
        }
    return {
        "found": True,
        "radiant_team": found.radiant_team,
        "dire_team": found.dire_team,
        "radiant_picks": found.radiant_picks,
        "dire_picks": found.dire_picks,
        "game_time": found.game_time,
        "delay": found.delay,
    }
