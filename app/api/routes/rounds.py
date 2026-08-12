"""Раунды: создание, сбор решений, исполнение, раскрытие.

Ключевая гарантия приватности: эндпоинт `/decisions` отдаёт содержимое решений
только после исполнения раунда. До этого возвращается лишь факт «кто уже подал».
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data import MarketNotAvailable
from app.api.schemas import ConfirmMapRequest, CreateRoundRequest, ManualDecisionRequest
from app.constants import ParticipantKey, Phase
from app.db.base import get_db
from app.db.models import (
    Decision,
    Market,
    Participant,
    RiskEvaluation,
    Round,
    SimulatedOrder,
    Snapshot,
)
from app.schemas.decision import TradeDecisionInput
from app.services import rounds as rounds_service
from app.services import snapshots as snapshot_service

router = APIRouter(prefix="/api/rounds", tags=["rounds"])


def _get_round(db: Session, round_id: int) -> Round:
    row = db.get(Round, round_id)
    if row is None:
        raise HTTPException(status_code=404, detail="раунд не найден")
    return row


@router.get("")
def list_rounds(db: Session = Depends(get_db)) -> list[dict]:
    result = []
    for row in db.scalars(select(Round).order_by(Round.id.desc())):
        market = db.get(Market, row.market_id)
        state = rounds_service.public_round_state(db, row)
        state["market_title"] = market.title if market else None
        result.append(state)
    return result


@router.post("", status_code=201)
def create_round(payload: CreateRoundRequest, db: Session = Depends(get_db)) -> dict:
    market = db.get(Market, payload.market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="рынок не найден")
    try:
        row = rounds_service.create_round(
            db,
            market,
            payload.phase,
            operator_context=payload.operator_context,
            map_number=payload.map_number,
            note=payload.note,
        )
    except MarketNotAvailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rounds_service.public_round_state(db, row)


@router.post("/between-maps", status_code=201)
def create_between_maps_round(
    market_id: int, payload: ConfirmMapRequest, db: Session = Depends(get_db)
) -> dict:
    """Оператор подтверждает окончание карты → новый snapshot и новый раунд.

    Решения, принятые по предыдущему snapshot, автоматически становятся
    устаревшими и будут отклонены risk engine.
    """
    market = db.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="рынок не найден")
    try:
        row = rounds_service.create_round(
            db,
            market,
            Phase.BETWEEN_MAPS,
            operator_context=payload.operator_context,
            map_number=payload.map_number,
            note=payload.note or f"подтверждено окончание карты {payload.map_number}",
        )
    except MarketNotAvailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return rounds_service.public_round_state(db, row)


@router.get("/{round_id}")
def get_round(round_id: int, db: Session = Depends(get_db)) -> dict:
    row = _get_round(db, round_id)
    market = db.get(Market, row.market_id)
    state = rounds_service.public_round_state(db, row)
    state["market"] = {
        "id": market.id,
        "title": market.title,
        "team_a": market.team_a,
        "team_b": market.team_b,
        "market_type": market.market_type,
    } if market else None
    return state


@router.get("/{round_id}/snapshot")
def get_snapshot(round_id: int, db: Session = Depends(get_db)) -> dict:
    """Единый снимок раунда — открыт всем участникам, это не секрет."""
    row = _get_round(db, round_id)
    snap = db.get(Snapshot, row.snapshot_id)
    stale, reason = snapshot_service.is_stale(snap)
    return {
        "snapshot_id": snap.id,
        "payload_hash": snap.payload_hash,
        "phase": snap.phase,
        "map_number": snap.map_number,
        "captured_at": snap.captured_at,
        "ttl_seconds": snap.ttl_seconds,
        "stale": stale,
        "stale_reason": reason,
        "payload": snap.payload,
    }


@router.post("/{round_id}/request-ai")
def request_ai(round_id: int, db: Session = Depends(get_db)) -> dict:
    """Одновременно запросить решения Codex и Claude по одному snapshot."""
    row = _get_round(db, round_id)
    try:
        stored = rounds_service.request_ai_decisions(db, row)
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # содержимое решений НЕ возвращаем — только факт получения
    return {
        "round_id": row.id,
        "collected": [db.get(Participant, d.participant_id).key for d in stored],
        "statuses": {db.get(Participant, d.participant_id).key: d.status for d in stored},
        "state": rounds_service.public_round_state(db, row),
    }


@router.post("/{round_id}/decisions/{participant_key}")
def submit_decision(
    round_id: int,
    participant_key: str,
    payload: ManualDecisionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Ручное решение (по умолчанию — Titan). После отправки редактирование запрещено."""
    row = _get_round(db, round_id)
    if participant_key != ParticipantKey.TITAN.value:
        raise HTTPException(
            status_code=400,
            detail="ручной ввод предусмотрен только для участника titan",
        )
    try:
        decision_input = TradeDecisionInput.model_validate(payload.model_dump())
    except Exception as exc:  # noqa: BLE001 — отдадим человеку понятную ошибку
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        decision = rounds_service.submit_manual_decision(
            db, row, participant_key, decision_input
        )
    except rounds_service.DecisionAlreadySubmitted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "decision_id": decision.id,
        "locked": decision.locked,
        "state": rounds_service.public_round_state(db, row),
        "note": "решение зафиксировано; чужие решения будут видны после исполнения раунда",
    }


@router.post("/{round_id}/execute")
def execute(round_id: int, db: Session = Depends(get_db)) -> dict:
    """Risk engine + paper execution для всех трёх решений."""
    row = _get_round(db, round_id)
    try:
        report = rounds_service.execute_round(db, row)
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"round_id": row.id, "status": row.status, "report": report}


@router.post("/{round_id}/reveal")
def reveal(round_id: int, db: Session = Depends(get_db)) -> dict:
    row = _get_round(db, round_id)
    try:
        rounds_service.reveal_round(db, row)
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_decisions(round_id, db)


@router.post("/{round_id}/cancel")
def cancel(round_id: int, reason: str = "отменено оператором", db: Session = Depends(get_db)) -> dict:
    row = _get_round(db, round_id)
    try:
        rounds_service.cancel_round(db, row, reason)
    except rounds_service.RoundStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return rounds_service.public_round_state(db, row)


@router.get("/{round_id}/decisions")
def get_decisions(round_id: int, db: Session = Depends(get_db)) -> dict:
    """Решения участников. До исполнения раунда содержимое СКРЫТО."""
    row = _get_round(db, round_id)
    state = rounds_service.public_round_state(db, row)
    if not rounds_service.is_revealed(row):
        return {
            **state,
            "hidden": True,
            "decisions": [],
            "message": (
                "решения скрыты до фиксации и исполнения раунда "
                f"(статус {row.status})"
            ),
        }

    items = []
    for decision in db.scalars(
        select(Decision).where(Decision.round_id == row.id).order_by(Decision.id)
    ):
        participant = db.get(Participant, decision.participant_id)
        evaluation = db.scalar(
            select(RiskEvaluation).where(RiskEvaluation.decision_id == decision.id)
        )
        order = db.scalar(select(SimulatedOrder).where(SimulatedOrder.decision_id == decision.id))
        items.append(
            {
                "participant": participant.key if participant else "?",
                "display_name": participant.display_name if participant else "?",
                "status": decision.status,
                "decision": decision.payload,
                "validation_error": decision.validation_error,
                "model_name": decision.model_name,
                "model_version": decision.model_version,
                "prompt_version": decision.prompt_version,
                "latency_ms": decision.latency_ms,
                "attempts": decision.attempts,
                "risk": {
                    "verdict": evaluation.verdict,
                    "requested_stake": evaluation.requested_stake,
                    "approved_stake": evaluation.approved_stake,
                    "reasons": evaluation.reasons,
                    "decision_before": evaluation.decision_before,
                    "decision_after": evaluation.decision_after,
                }
                if evaluation
                else None,
                "order": {
                    "id": order.id,
                    "status": order.status,
                    "outcome": order.outcome,
                    "filled_size": order.filled_size,
                    "avg_fill_price": order.avg_fill_price,
                    "notional": order.notional,
                    "fee": order.fee,
                    "slippage_bps": order.slippage_bps,
                    "reject_reason": order.reject_reason,
                }
                if order
                else None,
            }
        )
    return {**state, "hidden": False, "decisions": items}
