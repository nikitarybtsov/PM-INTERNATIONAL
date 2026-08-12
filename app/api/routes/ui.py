"""HTML-страницы панели оператора и формы Титана."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import LIVE_TRADING_ENABLED, get_settings
from app.constants import Phase
from app.db.base import get_db
from app.db.models import Market, Participant, Round, Settlement, Snapshot
from app.services import (
    export as export_service,
)
from app.services import (
    portfolio as pf,
)
from app.services import (
    rounds as rounds_service,
)
from app.services import (
    snapshots as snapshot_service,
)
from app.services import (
    stats as stats_service,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)


def _base_context(request: Request) -> dict:
    settings = get_settings()
    return {
        "request": request,
        "live_trading": LIVE_TRADING_ENABLED,
        "provider": settings.market_data_provider,
        "phases": [p.value for p in Phase],
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    board = stats_service.scoreboard(db)
    recent = []
    for row in db.scalars(select(Round).order_by(Round.id.desc()).limit(15)):
        market = db.get(Market, row.market_id)
        state = rounds_service.public_round_state(db, row)
        state["market_title"] = market.title if market else "—"
        recent.append(state)

    positions = []
    for participant in db.scalars(select(Participant).order_by(Participant.id)):
        for pos in pf.open_positions(db, participant.id):
            market = db.get(Market, pos.market_id)
            positions.append(
                {
                    "participant": participant.key,
                    "market": market.title if market else "—",
                    "outcome": pos.outcome,
                    "size": pos.size,
                    "avg_price": pos.avg_price,
                    "cost_basis": pos.cost_basis,
                }
            )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_base_context(request),
            "board": board,
            "recent_rounds": recent,
            "open_positions": positions,
            "chart_svg": export_service.equity_chart_svg(db),
            "disagreements": stats_service.disagreements(db, limit=5),
        },
    )


@router.get("/ui/markets", response_class=HTMLResponse)
def markets_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    markets = []
    for market in db.scalars(select(Market).order_by(Market.id)):
        settled = db.scalar(select(Settlement).where(Settlement.market_id == market.id))
        active = db.scalar(
            select(Round).where(
                Round.market_id == market.id, Round.status.in_(["OPEN", "LOCKED"])
            )
        )
        markets.append(
            {
                "row": market,
                "settled": settled.winning_outcome if settled else None,
                "active_round_id": active.id if active else None,
            }
        )
    return templates.TemplateResponse(
        "markets.html", {**_base_context(request), "markets": markets}
    )


@router.get("/ui/rounds/{round_id}", response_class=HTMLResponse)
def round_page(request: Request, round_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Страница оператора. Решения показываются только после исполнения."""
    round_row = db.get(Round, round_id)
    if round_row is None:
        raise HTTPException(status_code=404, detail="раунд не найден")
    snap = db.get(Snapshot, round_row.snapshot_id)
    stale, stale_reason = snapshot_service.is_stale(snap)
    market = db.get(Market, round_row.market_id)

    revealed = rounds_service.is_revealed(round_row)
    decisions = []
    if revealed:
        from app.api.routes.rounds import get_decisions

        decisions = get_decisions(round_id, db)["decisions"]

    return templates.TemplateResponse(
        "round.html",
        {
            **_base_context(request),
            "round": round_row,
            "market": market,
            "snapshot": snap,
            "snapshot_payload": snap.payload,
            "stale": stale,
            "stale_reason": stale_reason,
            "state": rounds_service.public_round_state(db, round_row),
            "revealed": revealed,
            "decisions": decisions,
        },
    )


@router.get("/ui/rounds/{round_id}/titan", response_class=HTMLResponse)
def titan_page(request: Request, round_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Форма Титана: виден ТОЛЬКО snapshot и собственный банк.

    Решения Codex и Claude на этой странице не отображаются ни при каких условиях
    до отправки собственного решения.
    """
    round_row = db.get(Round, round_id)
    if round_row is None:
        raise HTTPException(status_code=404, detail="раунд не найден")
    snap = db.get(Snapshot, round_row.snapshot_id)
    stale, stale_reason = snapshot_service.is_stale(snap)
    market = db.get(Market, round_row.market_id)

    titan = rounds_service.get_participant(db, "titan")
    portfolio = pf.get_portfolio(db, titan.id)
    submitted = rounds_service.existing_decision(db, round_row.id, titan.id)

    return templates.TemplateResponse(
        "titan.html",
        {
            **_base_context(request),
            "round": round_row,
            "market": market,
            "snapshot": snap,
            "payload": snap.payload,
            "stale": stale,
            "stale_reason": stale_reason,
            "portfolio": portfolio,
            "submitted": submitted,
            "submitted_payload": submitted.payload if submitted else None,
            "limits": get_settings().risk.model_dump(),
        },
    )


@router.get("/ui/exports", response_class=HTMLResponse)
def exports_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "exports.html",
        {**_base_context(request), "export_dir": get_settings().export_dir},
    )
