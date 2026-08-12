"""Цикл принятия решений.

Порядок строго зафиксирован:
  1. оператор выбирает рынок;
  2. система фиксирует snapshot (immutable, один на всех);
  3. параллельно запрашиваются Codex и Claude;
  4. система ждёт ручное решение Titan;
  5. все три решения валидируются;
  6. каждое проходит risk engine;
  7. одобренные заявки исполняются в paper engine;
  8. только после этого решения раскрываются.

До шага 8 ни API, ни интерфейс не отдают чужие решения.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.participants import get_participant_adapter
from app.adapters.participants.base import ParticipantResult
from app.adapters.participants.titan import TitanManualAdapter
from app.constants import (
    Action,
    DecisionStatus,
    ParticipantKey,
    Phase,
    RoundStatus,
)
from app.db.models import Decision, Market, Participant, Round, Snapshot
from app.schemas.decision import TradeDecision, TradeDecisionInput
from app.schemas.snapshot import MarketSnapshot
from app.services import audit, paper_engine, risk_engine, snapshots
from app.services import portfolio as pf

logger = logging.getLogger(__name__)

AI_PARTICIPANTS = (ParticipantKey.CODEX.value, ParticipantKey.CLAUDE.value)
ALL_PARTICIPANTS = (*AI_PARTICIPANTS, ParticipantKey.TITAN.value)


class RoundStateError(RuntimeError):
    """Операция недопустима в текущем статусе раунда."""


class DecisionAlreadySubmitted(RuntimeError):
    """Решение участника уже зафиксировано и не подлежит изменению."""


# ---------------------------------------------------------------------------
def get_participant(db: Session, key: str) -> Participant:
    participant = db.scalar(select(Participant).where(Participant.key == key))
    if participant is None:
        raise LookupError(f"участник {key} не найден — выполните seed")
    return participant


def create_round(
    db: Session,
    market: Market,
    phase: Phase,
    *,
    operator_context: str | None = None,
    map_number: int | None = None,
    note: str | None = None,
    actor: str = "operator",
) -> Round:
    """Шаги 1–2: зафиксировать snapshot и открыть раунд."""
    open_round = db.scalar(
        select(Round).where(
            Round.market_id == market.id,
            Round.status.in_([RoundStatus.OPEN.value, RoundStatus.LOCKED.value]),
        )
    )
    if open_round is not None:
        raise RoundStateError(
            f"по рынку {market.external_id} уже идёт раунд #{open_round.id} "
            f"в статусе {open_round.status}"
        )

    if phase == Phase.BETWEEN_MAPS and map_number is None:
        raise ValueError("для фазы BETWEEN_MAPS укажите номер завершённой карты")

    snapshot = snapshots.capture_snapshot(
        db, market, phase, operator_context=operator_context, map_number=map_number, actor=actor
    )
    round_row = Round(
        market_id=market.id,
        snapshot_id=snapshot.id,
        phase=phase.value,
        status=RoundStatus.OPEN.value,
        note=note,
    )
    db.add(round_row)
    db.flush()
    audit.record(
        db,
        entity_type="round",
        entity_id=round_row.id,
        action="create",
        actor=actor,
        after={"market_id": market.id, "snapshot_id": snapshot.id, "phase": phase.value},
    )
    return round_row


def snapshot_model_for(db: Session, round_row: Round) -> MarketSnapshot:
    snap = db.get(Snapshot, round_row.snapshot_id)
    if snap is None:  # pragma: no cover — защита от рассинхронизации
        raise LookupError(f"snapshot раунда #{round_row.id} не найден")
    return snapshots.load_snapshot_model(snap)


def existing_decision(db: Session, round_id: int, participant_id: int) -> Decision | None:
    return db.scalar(
        select(Decision).where(
            Decision.round_id == round_id, Decision.participant_id == participant_id
        )
    )


def decisions_for(db: Session, round_id: int) -> list[Decision]:
    return list(db.scalars(select(Decision).where(Decision.round_id == round_id)))


def submitted_keys(db: Session, round_id: int) -> set[str]:
    rows = (
        db.query(Participant.key)
        .join(Decision, Decision.participant_id == Participant.id)
        .filter(Decision.round_id == round_id)
        .all()
    )
    return {r[0] for r in rows}


def is_revealed(round_row: Round) -> bool:
    return round_row.status in (RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value)


# ---------------------------------------------------------------------------
def _store_decision(
    db: Session,
    round_row: Round,
    participant: Participant,
    result: ParticipantResult,
    snapshot_model: MarketSnapshot,
) -> Decision:
    outcome = result.decision.outcome
    market_probability = (
        snapshot_model.market_probability(outcome) if outcome else snapshot_model.yes_price
    )
    full = TradeDecision.build(
        decision=result.decision,
        participant_id=participant.key,
        snapshot_id=round_row.snapshot_id,
        market_id=round_row.market_id,
        market_probability=market_probability,
        model_name=result.model_name,
        model_version=result.model_version,
        prompt_version=result.prompt_version,
    )
    row = Decision(
        round_id=round_row.id,
        participant_id=participant.id,
        snapshot_id=round_row.snapshot_id,
        status=DecisionStatus.VALID.value,
        locked=True,
        payload=full.payload(),
        raw_response=result.raw_response,
        action=full.action.value,
        estimated_probability=full.estimated_probability,
        market_probability=full.market_probability,
        edge=full.edge,
        stake_usdc=full.stake_usdc,
        max_acceptable_price=full.max_acceptable_price,
        confidence=full.confidence,
        model_name=full.model_name,
        model_version=full.model_version,
        prompt_version=full.prompt_version,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        entity_type="decision",
        entity_id=row.id,
        action="submit",
        actor=participant.key,
        after={
            "action": row.action,
            "stake_usdc": row.stake_usdc,
            "estimated_probability": row.estimated_probability,
            "model": row.model_name,
        },
        note="решение зафиксировано и заблокировано от редактирования",
    )
    return row


def _store_failed_decision(
    db: Session,
    round_row: Round,
    participant: Participant,
    error: Exception,
    raw: str | None = None,
) -> Decision:
    row = Decision(
        round_id=round_row.id,
        participant_id=participant.id,
        snapshot_id=round_row.snapshot_id,
        status=DecisionStatus.FAILED.value,
        locked=True,
        payload={"error": str(error)[:2000]},
        raw_response=raw,
        validation_error=str(error)[:2000],
        action=Action.HOLD.value,
        stake_usdc=0.0,
        attempts=getattr(error, "attempts", 1),
    )
    db.add(row)
    db.flush()
    audit.log_api_error(
        db,
        provider=participant.adapter,
        error_type=getattr(error, "error_type", type(error).__name__),
        message=str(error),
        participant_id=participant.id,
        round_id=round_row.id,
        attempt=getattr(error, "attempts", 1),
    )
    audit.record(
        db,
        entity_type="decision",
        entity_id=row.id,
        action="failed",
        actor=participant.key,
        after={"error": str(error)[:500]},
    )
    return row


# ---------------------------------------------------------------------------
def request_ai_decisions(db: Session, round_row: Round, *, parallel: bool = True) -> list[Decision]:
    """Шаг 3: одновременно запросить Codex и Claude по одному и тому же snapshot."""
    if round_row.status != RoundStatus.OPEN.value:
        raise RoundStateError(
            f"запрашивать решения ИИ можно только в статусе OPEN (сейчас {round_row.status})"
        )

    snapshot_model = snapshot_model_for(db, round_row)
    pending: list[tuple[Participant, object]] = []
    for key in AI_PARTICIPANTS:
        participant = get_participant(db, key)
        if existing_decision(db, round_row.id, participant.id) is not None:
            continue
        view = pf.portfolio_view(db, participant, snapshot_model)
        pending.append((participant, view))

    if not pending:
        return []

    def _run(item):
        participant, view = item
        adapter = get_participant_adapter(participant.key)
        try:
            return participant, adapter.decide(snapshot_model, view), None
        except Exception as exc:  # noqa: BLE001 — сохраняем как FAILED-решение
            return participant, None, exc

    if parallel and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            outputs = list(pool.map(_run, pending))
    else:
        outputs = [_run(item) for item in pending]

    stored: list[Decision] = []
    for participant, result, error in outputs:
        if error is not None:
            stored.append(_store_failed_decision(db, round_row, participant, error))
        else:
            stored.append(_store_decision(db, round_row, participant, result, snapshot_model))

    _maybe_lock(db, round_row)
    return stored


def submit_manual_decision(
    db: Session,
    round_row: Round,
    participant_key: str,
    decision_input: TradeDecisionInput,
    *,
    actor: str | None = None,
) -> Decision:
    """Шаг 4: ручное решение Титана. Повторная отправка запрещена."""
    if round_row.status != RoundStatus.OPEN.value:
        raise RoundStateError(
            f"решение принимается только в статусе OPEN (сейчас {round_row.status})"
        )
    participant = get_participant(db, participant_key)
    if existing_decision(db, round_row.id, participant.id) is not None:
        raise DecisionAlreadySubmitted(
            f"{participant_key} уже отправил решение в раунде #{round_row.id}; "
            "изменение запрещено"
        )
    snapshot_model = snapshot_model_for(db, round_row)
    result = TitanManualAdapter.wrap_manual(decision_input)
    row = _store_decision(db, round_row, participant, result, snapshot_model)
    _maybe_lock(db, round_row)
    return row


def _maybe_lock(db: Session, round_row: Round) -> None:
    """Когда все три решения поданы — раунд запирается."""
    if round_row.status != RoundStatus.OPEN.value:
        return
    if submitted_keys(db, round_row.id) >= set(ALL_PARTICIPANTS):
        round_row.status = RoundStatus.LOCKED.value
        db.flush()
        audit.record(
            db,
            entity_type="round",
            entity_id=round_row.id,
            action="lock",
            after={"status": round_row.status},
            note="все три решения зафиксированы",
        )


# ---------------------------------------------------------------------------
def execute_round(db: Session, round_row: Round, *, actor: str = "operator") -> dict:
    """Шаги 5–7: валидация, risk engine, симуляция исполнения."""
    if round_row.status == RoundStatus.EXECUTED.value:
        raise RoundStateError(f"раунд #{round_row.id} уже исполнен")
    if round_row.status == RoundStatus.REVEALED.value:
        raise RoundStateError(f"раунд #{round_row.id} уже раскрыт")
    if round_row.status != RoundStatus.LOCKED.value:
        raise RoundStateError(
            f"исполнение возможно только после фиксации всех решений "
            f"(статус {round_row.status})"
        )

    snapshot_row = db.get(Snapshot, round_row.snapshot_id)
    snapshot_model = snapshots.load_snapshot_model(snapshot_row)
    report: dict[str, dict] = {}

    for decision in decisions_for(db, round_row.id):
        participant = db.get(Participant, decision.participant_id)
        ctx = risk_engine.build_context(db, decision, snapshot_row, snapshot_model, participant)
        outcome = risk_engine.evaluate(ctx)
        risk_engine.persist_evaluation(db, decision, ctx, outcome)

        entry: dict = {
            "participant": participant.key,
            "verdict": outcome.verdict.value,
            "requested_stake": ctx.stake_usdc,
            "approved_stake": outcome.approved_stake,
            "reasons": outcome.reasons_payload(),
            "executed": False,
        }
        try:
            execution = paper_engine.execute(
                db,
                decision=decision,
                risk=outcome,
                snapshot_row=snapshot_row,
                snapshot_model=snapshot_model,
            )
        except paper_engine.DuplicateExecution as exc:
            entry["error"] = str(exc)
            execution = None
        if execution is not None:
            entry.update(
                {
                    "executed": True,
                    "order_id": execution.order.id,
                    "status": execution.status.value,
                    "filled_size": execution.filled_size,
                    "avg_price": execution.avg_price,
                    "notional": execution.notional,
                    "fee": execution.fee,
                }
            )
        report[participant.key] = entry

    round_row.status = RoundStatus.EXECUTED.value
    round_row.executed_at = datetime.now(UTC)
    db.flush()
    audit.record(
        db,
        entity_type="round",
        entity_id=round_row.id,
        action="execute",
        actor=actor,
        after=report,
    )
    return report


def reveal_round(db: Session, round_row: Round, *, actor: str = "operator") -> Round:
    """Шаг 8: раскрыть сравнительную таблицу."""
    if round_row.status not in (RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value):
        raise RoundStateError("раскрытие возможно только после исполнения раунда")
    if round_row.status == RoundStatus.EXECUTED.value:
        round_row.status = RoundStatus.REVEALED.value
        round_row.revealed_at = datetime.now(UTC)
        db.flush()
        audit.record(
            db, entity_type="round", entity_id=round_row.id, action="reveal", actor=actor
        )
    return round_row


def cancel_round(db: Session, round_row: Round, reason: str, *, actor: str = "operator") -> Round:
    if round_row.status in (RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value):
        raise RoundStateError("исполненный раунд отменить нельзя")
    round_row.status = RoundStatus.CANCELLED.value
    round_row.note = reason
    db.flush()
    audit.record(
        db,
        entity_type="round",
        entity_id=round_row.id,
        action="cancel",
        actor=actor,
        after={"reason": reason},
    )
    return round_row


# ---------------------------------------------------------------------------
def public_round_state(db: Session, round_row: Round) -> dict:
    """Состояние раунда БЕЗ раскрытия чужих решений."""
    keys = submitted_keys(db, round_row.id)
    return {
        "round_id": round_row.id,
        "market_id": round_row.market_id,
        "snapshot_id": round_row.snapshot_id,
        "phase": round_row.phase,
        "status": round_row.status,
        "submitted": sorted(keys),
        "awaiting": sorted(set(ALL_PARTICIPANTS) - keys),
        "revealed": is_revealed(round_row),
        "created_at": round_row.created_at,
        "executed_at": round_row.executed_at,
        "revealed_at": round_row.revealed_at,
    }
