"""HTML-страницы панели оператора и формы Титана."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.constants import Phase, RoundStatus
from app.db.base import get_db
from app.db.models import (
    Market,
    Participant,
    RiskEvaluation,
    Round,
    Settlement,
    Snapshot,
)
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
        "live_trading": settings.live_execution_ready(),
        "execution_mode": (
            "LIVE" if settings.live_execution_ready()
            else "DRY-RUN" if settings.live_trading_enabled
            else "PAPER"
        ),
        "provider": settings.market_data_provider,
        "titan_participates": settings.titan_participates_in_rounds,
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

    # Оператор видит заявки, когда зафиксировались ВСЕ трое: до этого показ
    # означал бы, что он может подсказать Титану, что выбрали ИИ.
    can_review = round_row.status in (
        RoundStatus.LOCKED.value,
        RoundStatus.AWAITING_APPROVAL.value,
        RoundStatus.EXECUTED.value,
        RoundStatus.REVEALED.value,
    )

    decisions = []
    if can_review:
        from app.api.routes.rounds import get_decisions

        payload = get_decisions(round_id, db)
        decisions = payload.get("decisions", []) if not payload.get("hidden") else []
        if not decisions:
            decisions = _operator_view(db, round_row)

    settings = get_settings()
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
            "can_review": can_review,
            "awaiting_approval": round_row.status == RoundStatus.AWAITING_APPROVAL.value,
            "approval_required": settings.live_execution_ready(),
            "decisions": decisions,
        },
    )


def _operator_view(db: Session, round_row: Round) -> list[dict]:
    """Заявки для экрана одобрения: решение, вердикт риск-движка, одобрение.

    Отдельно от `get_decisions`, потому что тот отдаёт данные участникам и до
    раскрытия молчит. Оператор — не участник: ему нужно видеть, что он одобряет.
    """
    from app.db.models import Participant, RiskEvaluation

    rows = []
    for decision in rounds_service.decisions_for(db, round_row.id):
        participant = db.get(Participant, decision.participant_id)
        evaluation = db.scalar(
            select(RiskEvaluation).where(RiskEvaluation.decision_id == decision.id)
        )
        rows.append(
            {
                "participant": participant.key if participant else "?",
                "display_name": participant.display_name if participant else "?",
                "status": decision.status,
                "decision": decision.payload or {},
                "model_name": decision.model_name,
                "net_edge": decision.net_edge,
                "taker_fee_usdc": decision.taker_fee_usdc,
                "approved_by": decision.approved_by,
                "risk": {
                    "verdict": evaluation.verdict if evaluation else None,
                    "approved_stake": evaluation.approved_stake if evaluation else None,
                    "reasons": evaluation.reasons if evaluation else [],
                }
                if evaluation
                else None,
            }
        )
    return sorted(rows, key=lambda r: r["participant"])


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


@router.get("/ui/rounds", response_class=HTMLResponse)
def rounds_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Список раундов: по каким матчам модели уже высказались и что ждёт вас."""
    rows = []
    for round_row in db.scalars(select(Round).order_by(Round.id.desc()).limit(100)):
        market = db.get(Market, round_row.market_id)
        state = rounds_service.public_round_state(db, round_row)
        decisions = rounds_service.decisions_for(db, round_row.id)

        # Сколько заявок реально ждёт одобрения — это главное, зачем сюда заходят
        pending = 0
        for decision in decisions:
            if decision.action in (None, "HOLD") or decision.approved_by:
                continue
            evaluation = db.scalar(
                select(RiskEvaluation).where(RiskEvaluation.decision_id == decision.id)
            )
            if evaluation is not None and evaluation.verdict != "REJECTED":
                pending += 1

        rows.append(
            {
                "id": round_row.id,
                "market": market.title if market else "?",
                "market_type": market.market_type if market else "",
                "starts_at": market.starts_at if market else None,
                "phase": round_row.phase,
                "status": round_row.status,
                "submitted": state["submitted"],
                "awaiting": state["awaiting"],
                "pending_approval": pending,
                "created_at": round_row.created_at,
            }
        )
    return templates.TemplateResponse(
        "rounds.html", {**_base_context(request), "rounds": rows}
    )


@router.get("/ui/trades", response_class=HTMLResponse)
def trades_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Лента совершённых сделок: что, когда, по какой цене и кто одобрил."""
    from app.db.models import Fill, SimulatedOrder

    rows = []
    orders = db.scalars(
        select(SimulatedOrder).order_by(SimulatedOrder.id.desc()).limit(200)
    )
    for order in orders:
        participant = db.get(Participant, order.participant_id)
        market = db.get(Market, order.market_id)
        fills = list(db.scalars(select(Fill).where(Fill.order_id == order.id)))
        filled = round(sum(f.size for f in fills), 6)
        notional = round(sum(f.size * f.price for f in fills), 4)
        rows.append(
            {
                "id": order.id,
                "round_id": order.round_id,
                "participant": participant.key if participant else "?",
                "market": market.title if market else "?",
                "market_type": market.market_type if market else "",
                "action": order.action,
                "outcome": order.outcome,
                "status": order.status,
                "filled_size": filled,
                "avg_price": round(notional / filled, 4) if filled else 0.0,
                "notional": notional,
                "fee": order.fee or 0.0,
                "created_at": order.created_at,
            }
        )

    return templates.TemplateResponse(
        "trades.html",
        {
            **_base_context(request),
            "orders": rows,
            "titan_address": get_settings().titan_poly_address,
        },
    )


@router.get("/ui/exports", response_class=HTMLResponse)
def exports_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "exports.html",
        {**_base_context(request), "export_dir": get_settings().export_dir},
    )
