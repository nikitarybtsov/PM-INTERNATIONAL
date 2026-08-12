"""Тесты автоматического планировщика раундов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.adapters.notifiers.base import NullNotifier
from app.adapters.notifiers.factory import set_notifier_override
from app.config import reset_settings_cache
from app.constants import Phase, RoundStatus
from app.db.models import Market, Round
from app.schemas.decision import TradeDecisionInput
from app.services import rounds as rounds_service
from app.services import scheduler as sched

TITAN_BUY = TradeDecisionInput(
    action="BUY_YES",
    estimated_probability=0.7,
    stake_usdc=50,
    max_acceptable_price=0.95,
    confidence=0.8,
    short_reason="ставлю на фаворита",
)


@pytest.fixture(autouse=True)
def _notifier():
    sink = NullNotifier()
    set_notifier_override(sink)
    yield sink
    set_notifier_override(None)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def frozen_markets(monkeypatch):
    """Отключить обновление рынков внутри тика.

    Тик всегда начинается с `refresh_markets`, а тот перезаписывает метаданные
    рынка данными провайдера — включая `starts_at`. Тестам, которые сами
    выставляют время старта, обновление надо отключить.
    """
    monkeypatch.setattr(sched, "refresh_markets", lambda db: 0)


def _set_start(db: Session, market: Market, minutes_from_now: int) -> Market:
    market.starts_at = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    db.flush()
    return market


# --- отбор кандидатов -------------------------------------------------------
def test_disabled_scheduler_does_nothing(db: Session, seeded, market: Market):
    result = sched.tick(db)
    assert result.rounds_opened == []
    assert result.markets_refreshed == 0


def test_candidate_inside_window(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 120)
    assert market.id in {m.id for m in sched.candidate_markets(db)}


def test_candidate_too_far_is_skipped(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 60 * 24 * 3)
    assert market.id not in {m.id for m in sched.candidate_markets(db)}


def test_candidate_too_close_is_skipped(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 5)  # ближе, чем min_before_match_minutes=20
    assert market.id not in {m.id for m in sched.candidate_markets(db)}


def test_market_with_open_round_is_skipped(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 120)
    rounds_service.create_round(db, market, Phase.PREMATCH)
    assert market.id not in {m.id for m in sched.candidate_markets(db)}


def test_settled_market_is_skipped(db: Session, seeded, market: Market, enabled):
    from app.services import settlement

    _set_start(db, market, 120)
    settlement.settle_market(db, market, "YES")
    assert market.id not in {m.id for m in sched.candidate_markets(db)}


# --- открытие раундов -------------------------------------------------------
def test_tick_opens_round_and_collects_ai(db: Session, seeded, market: Market, enabled, frozen_markets):
    for m in db.query(Market).all():
        _set_start(db, m, 120)

    result = sched.tick(db)
    assert result.rounds_opened

    round_row = db.get(Round, result.rounds_opened[0])
    assert round_row.status == RoundStatus.OPEN.value
    state = rounds_service.public_round_state(db, round_row)
    assert set(state["submitted"]) == {"codex", "claude"}
    assert state["awaiting"] == ["titan"]


def test_tick_respects_max_open_rounds(db: Session, seeded, market: Market, enabled, frozen_markets, monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_OPEN_ROUNDS", "2")
    reset_settings_cache()
    for m in db.query(Market).all():
        _set_start(db, m, 120)

    result = sched.tick(db)
    assert len(result.rounds_opened) <= 2
    reset_settings_cache()


def test_tick_is_idempotent(db: Session, seeded, market: Market, enabled, frozen_markets, monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_OPEN_ROUNDS", "1")
    reset_settings_cache()
    for m in db.query(Market).all():
        _set_start(db, m, 120)

    first = sched.tick(db)
    second = sched.tick(db)
    assert len(first.rounds_opened) == 1
    assert second.rounds_opened == []  # свободных слотов нет
    reset_settings_cache()


# --- завершение раунда ------------------------------------------------------
def test_locked_round_is_executed(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 120)
    round_row = sched.open_round(db, market)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    assert round_row.status == RoundStatus.LOCKED.value

    action = sched.resolve_pending(db, round_row)
    assert action == "executed"
    assert round_row.status == RoundStatus.REVEALED.value


def test_deadline_cancels_round_by_default(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 120)
    round_row = sched.open_round(db, market)

    future = datetime.now(UTC) + timedelta(hours=3)
    action = sched.resolve_pending(db, round_row, now=future)

    assert action == "cancelled"
    assert round_row.status == RoundStatus.CANCELLED.value
    # решение за Титана не выдумано
    titan = rounds_service.get_participant(db, "titan")
    assert rounds_service.existing_decision(db, round_row.id, titan.id) is None


def test_deadline_hold_policy_executes(db: Session, seeded, market: Market, enabled, monkeypatch):
    monkeypatch.setenv("TITAN_TIMEOUT_POLICY", "hold")
    reset_settings_cache()
    _set_start(db, market, 120)
    round_row = sched.open_round(db, market)

    future = datetime.now(UTC) + timedelta(hours=3)
    action = sched.resolve_pending(db, round_row, now=future)

    assert action == "executed"
    titan = rounds_service.get_participant(db, "titan")
    decision = rounds_service.existing_decision(db, round_row.id, titan.id)
    assert decision is not None
    assert decision.action == "HOLD"
    reset_settings_cache()


def test_reminder_before_deadline(db: Session, seeded, market: Market, enabled, _notifier):
    _set_start(db, market, 120)
    round_row = sched.open_round(db, market)

    deadline = sched._deadline_for(db, round_row)
    almost = deadline - timedelta(minutes=3)
    action = sched.resolve_pending(db, round_row, now=almost)

    assert action == "reminded"
    assert any("дедлайна" in m for m in _notifier.messages)


def test_no_action_long_before_deadline(db: Session, seeded, market: Market, enabled):
    _set_start(db, market, 120)
    round_row = sched.open_round(db, market)
    assert sched.resolve_pending(db, round_row) is None
    assert round_row.status == RoundStatus.OPEN.value


# --- отказоустойчивость -----------------------------------------------------
def test_tick_survives_adapter_failure(db: Session, seeded, market: Market, enabled, frozen_markets):
    from app.adapters.participants.base import ParticipantAdapter, ParticipantError
    from app.adapters.participants.factory import set_adapter_override

    class Broken(ParticipantAdapter):
        key = "codex"

        def decide(self, snapshot, portfolio):
            raise ParticipantError("недоступен", error_type="timeout")

    set_adapter_override("codex", Broken())
    for m in db.query(Market).all():
        _set_start(db, m, 120)

    result = sched.tick(db)
    assert result.rounds_opened  # раунд всё равно открыт
    round_row = db.get(Round, result.rounds_opened[0])
    failed = [d for d in rounds_service.decisions_for(db, round_row.id) if d.status == "FAILED"]
    assert failed


def test_tick_reports_errors_without_raising(db: Session, seeded, market: Market, enabled, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("провайдер лёг")

    monkeypatch.setattr(sched, "refresh_markets", boom)
    result = sched.tick(db)
    assert any("refresh" in e for e in result.errors)
