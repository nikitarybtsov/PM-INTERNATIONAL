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

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.participants import get_participant_adapter
from app.adapters.participants.base import ParticipantResult
from app.adapters.participants.titan import TitanManualAdapter
from app.config import get_settings
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


def expected_participants() -> tuple[str, ...]:
    """Кого раунд ждёт перед исполнением.

    Титан торгует со своего кошелька руками, и его сделки подтягиваются с
    биржи. Если он не подаёт решения через форму, раунд не должен висеть в
    ожидании: два ИИ отработали — можно одобрять и исполнять.
    """
    if get_settings().titan_participates_in_rounds:
        return ALL_PARTICIPANTS
    return AI_PARTICIPANTS


class RoundStateError(RuntimeError):
    """Операция недопустима в текущем статусе раунда."""


class DecisionAlreadySubmitted(RuntimeError):
    """Решение участника уже зафиксировано и не подлежит изменению."""


def decision_fingerprint(payload: dict) -> str:
    """SHA-256 содержимого решения.

    Пишется в аудит вместо самого решения: доказывает неизменность записи,
    ничего не раскрывая. Сверить можно после раскрытия раунда.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


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
    """Шаги 1–2: зафиксировать snapshot и открыть раунд.

    Второй раунд по тому же рынку недопустим: новый снимок обесценивает решения
    предыдущего (risk engine отклонит их как устаревшие), и работа моделей
    пропадает впустую. Именно так сгорели заявки, пока Codex и Claude думали.
    """
    active = db.scalar(
        select(Round).where(
            Round.market_id == market.id,
            Round.status.in_(
                [
                    RoundStatus.OPEN.value,
                    RoundStatus.LOCKED.value,
                    # раунд ждёт одобрения оператора — он всё ещё живой
                    RoundStatus.AWAITING_APPROVAL.value,
                ]
            ),
        )
    )
    if active is not None:
        raise RoundStateError(
            f"по рынку {market.external_id} уже идёт раунд #{active.id} "
            f"в статусе {active.status}. Завершите или отмените его."
        )

    # Защита от двойного клика по кнопке. Отмена раунда под неё не подпадает:
    # это осознанное действие оператора, после него сразу можно начать заново.
    cooldown = get_settings().round_create_cooldown_seconds
    if cooldown > 0:
        # Только та же фаза: переход PREMATCH → BETWEEN_MAPS после конца карты
        # и повтор после отмены — осознанные действия, их блокировать нельзя.
        recent = db.scalar(
            select(Round)
            .where(
                Round.market_id == market.id,
                Round.phase == phase.value,
                Round.status != RoundStatus.CANCELLED.value,
            )
            .order_by(Round.id.desc())
            .limit(1)
        )
        if recent is not None and recent.created_at is not None:
            created = recent.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - created).total_seconds()
            if age < cooldown:
                raise RoundStateError(
                    f"раунд #{recent.id} по этому рынку создан {age:.0f} с назад. "
                    f"Повторное создание доступно через {cooldown - age:.0f} с — "
                    f"это защита от случайного двойного нажатия."
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
    target_market_id = result.decision.target_market_id
    if not snapshot_model.allows_market(target_market_id):
        # Участник назвал рынок, которого нет в снимке. Молча подменять его на
        # основной нельзя: оценка вероятности относилась к другому событию.
        raise ValueError(
            f"рынок {target_market_id} отсутствует в snapshot; "
            f"доступны: {snapshot_model.tradeable_market_ids()}"
        )
    market_probability = (
        snapshot_model.market_probability(outcome, target_market_id)
        if outcome
        else snapshot_model.yes_price
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
        net_edge=full.net_edge,
        taker_fee_usdc=full.taker_fee_usdc,
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
        # В аудит пишется отпечаток, а не содержимое: журнал доступен по API и не
        # должен раскрывать чужое решение до фиксации. Хеша достаточно, чтобы
        # доказать, что сохранённое решение не переписали задним числом.
        after={
            "decision_fingerprint": decision_fingerprint(row.payload),
            "model": row.model_name,
            "prompt_version": row.prompt_version,
            "locked": row.locked,
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

    # Модели думают минутами. Всё это время открытая транзакция держала бы
    # SQLite заблокированной, и любая параллельная запись — например, фиксация
    # снимка по другому матчу — падала бы с «database is locked», а оператор
    # видел бы Internal Server Error. Поэтому отпускаем базу до вызова и
    # берём заново после: снимок уже прочитан и в памяти, он неизменяем.
    db.commit()

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
    """Когда все ожидаемые решения поданы — раунд запирается."""
    if round_row.status != RoundStatus.OPEN.value:
        return
    if submitted_keys(db, round_row.id) >= set(expected_participants()):
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
def approve_decision(
    db: Session, round_row: Round, participant_key: str, *, actor: str = "operator"
) -> Decision:
    """Оператор одобряет конкретную заявку к исполнению.

    В боевом режиме это третий предохранитель: без одобрения ордер не уйдёт
    на биржу. Одобрение фиксируется в аудите и не может быть отозвано молча —
    снятие пишет отдельную запись.
    """
    if round_row.status in (RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value):
        raise RoundStateError("раунд уже исполнен, одобрять нечего")

    participant = get_participant(db, participant_key)
    decision = db.scalar(
        select(Decision).where(
            Decision.round_id == round_row.id,
            Decision.participant_id == participant.id,
        )
    )
    if decision is None:
        raise RoundStateError(f"решение участника {participant_key} не найдено")
    if decision.status != DecisionStatus.VALID.value:
        raise RoundStateError(
            f"решение участника {participant_key} невалидно ({decision.status})"
        )

    decision.approved_by = actor
    decision.approved_at = datetime.now(UTC)
    db.flush()
    audit.record(
        db,
        entity_type="decision",
        entity_id=decision.id,
        action="approve",
        actor=actor,
        after={"participant": participant_key, "approved_at": decision.approved_at.isoformat()},
        note="оператор одобрил заявку к исполнению",
    )
    return decision


def revoke_approval(
    db: Session, round_row: Round, participant_key: str, *, actor: str = "operator"
) -> Decision:
    """Снять одобрение до исполнения."""
    if round_row.status in (RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value):
        raise RoundStateError("раунд уже исполнен")
    participant = get_participant(db, participant_key)
    decision = db.scalar(
        select(Decision).where(
            Decision.round_id == round_row.id,
            Decision.participant_id == participant.id,
        )
    )
    if decision is None:
        raise RoundStateError(f"решение участника {participant_key} не найдено")
    decision.approved_by = None
    decision.approved_at = None
    db.flush()
    audit.record(
        db,
        entity_type="decision",
        entity_id=decision.id,
        action="revoke_approval",
        actor=actor,
        after={"participant": participant_key},
        note="оператор снял одобрение",
    )
    return decision


def prepare_round(db: Session, round_row: Round, *, actor: str = "operator") -> dict:
    """Прогнать заявки через risk engine и остановиться перед исполнением.

    Отдельный шаг нужен, чтобы оператор видел, что именно уйдёт на биржу:
    какой размер одобрил risk engine, по какой цене и с какой комиссией.
    """
    if round_row.status not in (RoundStatus.LOCKED.value, RoundStatus.AWAITING_APPROVAL.value):
        raise RoundStateError(
            f"подготовка возможна после фиксации решений (статус {round_row.status})"
        )

    snapshot_row = db.get(Snapshot, round_row.snapshot_id)
    snapshot_model = snapshots.load_snapshot_model(snapshot_row)
    proposals: dict[str, dict] = {}

    for decision in decisions_for(db, round_row.id):
        participant = db.get(Participant, decision.participant_id)
        ctx = risk_engine.build_context(db, decision, snapshot_row, snapshot_model, participant)
        outcome = risk_engine.evaluate(ctx)
        risk_engine.persist_evaluation(db, decision, ctx, outcome)
        proposals[participant.key] = {
            "participant": participant.key,
            "verdict": outcome.verdict.value,
            "requested_stake": ctx.stake_usdc,
            "approved_stake": outcome.approved_stake,
            "approved_size": outcome.approved_size,
            "outcome": outcome.outcome,
            "expected_avg_price": outcome.expected_avg_price,
            "reasons": outcome.reasons_payload(),
            "executable": outcome.is_executable,
            "approved_by": decision.approved_by,
            "net_edge": decision.net_edge,
            "taker_fee_usdc": decision.taker_fee_usdc,
        }

    round_row.status = RoundStatus.AWAITING_APPROVAL.value
    db.flush()
    audit.record(
        db,
        entity_type="round",
        entity_id=round_row.id,
        action="prepare",
        actor=actor,
        after={"proposals": {k: v["verdict"] for k, v in proposals.items()}},
        note="risk engine отработал, ожидается одобрение оператора",
    )
    return proposals


def execute_round(db: Session, round_row: Round, *, actor: str = "operator") -> dict:
    """Шаги 5–7: валидация, risk engine, исполнение.

    В боевом режиме исполняются ТОЛЬКО заявки, одобренные оператором;
    остальные пропускаются с пометкой. В бумажном режиме одобрение не
    требуется — там нет денег и подтверждать нечего.
    """
    if round_row.status == RoundStatus.EXECUTED.value:
        raise RoundStateError(f"раунд #{round_row.id} уже исполнен")
    if round_row.status == RoundStatus.REVEALED.value:
        raise RoundStateError(f"раунд #{round_row.id} уже раскрыт")
    if round_row.status not in (RoundStatus.LOCKED.value, RoundStatus.AWAITING_APPROVAL.value):
        raise RoundStateError(
            f"исполнение возможно только после фиксации всех решений "
            f"(статус {round_row.status})"
        )

    live = get_settings().live_execution_ready()

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
            "approved_by": decision.approved_by,
        }

        # Боевой режим: без одобрения оператора заявка не исполняется.
        if live and outcome.is_executable and not decision.approved_by:
            entry["skipped"] = "не одобрено оператором"
            report[participant.key] = entry
            continue

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
        "awaiting": sorted(set(expected_participants()) - keys),
        "revealed": is_revealed(round_row),
        "created_at": round_row.created_at,
        "executed_at": round_row.executed_at,
        "revealed_at": round_row.revealed_at,
    }
