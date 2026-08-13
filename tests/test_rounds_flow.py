"""Тесты цикла раунда: статусы, устаревший snapshot, повторное исполнение, фазы."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.constants import Phase, RiskVerdict, RoundStatus
from app.db.models import Market, Round
from app.schemas.decision import TradeDecisionInput
from app.services import rounds as rounds_service
from app.services import snapshots as snapshot_service

TITAN_HOLD = TradeDecisionInput(
    action="HOLD",
    estimated_probability=0.5,
    stake_usdc=0,
    confidence=0.5,
    short_reason="пропускаю раунд",
)

TITAN_BUY = TradeDecisionInput(
    action="BUY_YES",
    estimated_probability=0.7,
    stake_usdc=60,
    max_acceptable_price=0.95,
    confidence=0.8,
    short_reason="считаю фаворита недооценённым",
    key_factors=["опыт игры на Титане", "форма команды"],
    risk_factors=["возможна замена"],
    information_used=["snapshot"],
)


def full_round(db: Session, market: Market, titan_decision=TITAN_BUY, phase=Phase.PREMATCH):
    round_row = rounds_service.create_round(db, market, phase, map_number=1 if phase == Phase.BETWEEN_MAPS else None)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", titan_decision)
    return round_row


# --- статусы ----------------------------------------------------------------
def test_round_status_machine(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    assert round_row.status == RoundStatus.OPEN.value

    rounds_service.request_ai_decisions(db, round_row)
    assert round_row.status == RoundStatus.OPEN.value  # ждём Титана

    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_HOLD)
    assert round_row.status == RoundStatus.LOCKED.value

    rounds_service.execute_round(db, round_row)
    assert round_row.status == RoundStatus.EXECUTED.value

    rounds_service.reveal_round(db, round_row)
    assert round_row.status == RoundStatus.REVEALED.value


def test_cannot_execute_before_all_decisions(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    with pytest.raises(rounds_service.RoundStateError):
        rounds_service.execute_round(db, round_row)


def test_double_execution_is_blocked(db: Session, seeded, market: Market):
    round_row = full_round(db, market)
    rounds_service.execute_round(db, round_row)
    with pytest.raises(rounds_service.RoundStateError):
        rounds_service.execute_round(db, round_row)


def test_decision_cannot_be_resubmitted(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_HOLD)
    with pytest.raises(rounds_service.DecisionAlreadySubmitted):
        rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)


def test_ai_decisions_are_not_requested_twice(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    first = rounds_service.request_ai_decisions(db, round_row)
    second = rounds_service.request_ai_decisions(db, round_row)
    assert len(first) == 2
    assert second == []


def test_only_one_active_round_per_market(db: Session, seeded, market: Market):
    rounds_service.create_round(db, market, Phase.PREMATCH)
    with pytest.raises(rounds_service.RoundStateError):
        rounds_service.create_round(db, market, Phase.PREMATCH)


def test_between_maps_requires_map_number(db: Session, seeded, market: Market):
    with pytest.raises(ValueError):
        rounds_service.create_round(db, market, Phase.BETWEEN_MAPS)


def test_between_maps_round_is_created_after_operator_confirmation(
    db: Session, seeded, market: Market
):
    round_row = rounds_service.create_round(db, market, Phase.BETWEEN_MAPS, map_number=2)
    assert round_row.phase == Phase.BETWEEN_MAPS.value
    assert round_row.snapshot.map_number == 2


# --- устаревший snapshot ----------------------------------------------------
def test_new_snapshot_supersedes_previous(db: Session, seeded, market: Market):
    first = snapshot_service.capture_snapshot(db, market, Phase.PREMATCH)
    stale, reason = snapshot_service.is_stale(first)
    assert stale is False

    second = snapshot_service.capture_snapshot(db, market, Phase.BETWEEN_MAPS, map_number=1)
    db.refresh(first)
    stale, reason = snapshot_service.is_stale(first)
    assert stale is True
    assert str(second.id) in reason


def test_decisions_on_stale_snapshot_are_rejected(db: Session, seeded, market: Market):
    """Решения по старому snapshot отклоняются как устаревшие."""
    round_row = full_round(db, market)

    # оператор подтверждает окончание карты → новый snapshot
    snapshot_service.capture_snapshot(db, market, Phase.BETWEEN_MAPS, map_number=1)

    report = rounds_service.execute_round(db, round_row)
    for entry in report.values():
        assert entry["verdict"] == RiskVerdict.REJECTED.value
        assert any(r["code"] == "stale_snapshot" for r in entry["reasons"])
        assert entry["executed"] is False


def test_ttl_expiry_marks_snapshot_stale(db: Session, seeded, market: Market, monkeypatch):
    from datetime import UTC, datetime, timedelta

    snap = snapshot_service.capture_snapshot(db, market, Phase.PREMATCH)
    future = datetime.now(UTC) + timedelta(seconds=snap.ttl_seconds + 60)
    stale, reason = snapshot_service.is_stale(snap, now=future)
    assert stale is True
    assert "TTL" in reason


# --- исполнение -------------------------------------------------------------
def test_execute_produces_orders_and_reasons(db: Session, seeded, market: Market):
    round_row = full_round(db, market)
    report = rounds_service.execute_round(db, round_row)

    assert set(report) == {"codex", "claude", "titan"}
    for entry in report.values():
        assert entry["reasons"], "у каждого решения должна быть причина в журнале"
        assert entry["verdict"] in {v.value for v in RiskVerdict}

    titan = report["titan"]
    assert titan["executed"] is True
    assert titan["filled_size"] > 0


def test_cancel_round(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.cancel_round(db, round_row, "матч перенесён")
    assert round_row.status == RoundStatus.CANCELLED.value
    # после отмены можно создать новый раунд по тому же рынку
    new_round = rounds_service.create_round(db, market, Phase.PREMATCH)
    assert isinstance(new_round, Round)


# --- запрет дублей раундов --------------------------------------------------
def test_second_round_blocked_while_first_awaits_approval(db: Session, market: Market):
    """Раунд в AWAITING_APPROVAL всё ещё живой — второй по тому же рынку нельзя.

    Раньше проверка смотрела только OPEN и LOCKED, из-за чего оператор открывал
    дубль, а решения первого раунда сгорали как устаревшие.
    """
    first = rounds_service.create_round(db, market, Phase.PREMATCH)
    first.status = RoundStatus.AWAITING_APPROVAL.value
    db.flush()

    with pytest.raises(rounds_service.RoundStateError, match="уже идёт раунд"):
        rounds_service.create_round(db, market, Phase.PREMATCH)


def test_double_click_is_blocked_by_cooldown(db: Session, market: Market):
    """Повторное нажатие сразу после исполненного раунда — почти наверняка промах."""
    first = rounds_service.create_round(db, market, Phase.PREMATCH)
    first.status = RoundStatus.EXECUTED.value
    db.flush()

    with pytest.raises(rounds_service.RoundStateError, match="двойного нажатия"):
        rounds_service.create_round(db, market, Phase.PREMATCH)


def test_cancelled_round_does_not_block_new_one(db: Session, market: Market):
    """Отмена — осознанное действие: после неё раунд создаётся сразу."""
    first = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.cancel_round(db, first, "передумал")
    db.flush()

    second = rounds_service.create_round(db, market, Phase.PREMATCH)
    assert second.id != first.id
