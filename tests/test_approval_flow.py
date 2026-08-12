"""Одобрение оператора между risk engine и исполнением.

Главное, что здесь проверяется: в боевом режиме деньги не двигаются, пока
человек не нажал кнопку. В бумажном режиме подтверждать нечего, поэтому
поведение остаётся прежним.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.config import reset_settings_cache
from app.constants import Action, DecisionStatus, Phase, RoundStatus
from app.db.models import Decision, Market
from app.schemas.decision import TradeDecisionInput
from app.services import rounds as rounds_service

TITAN_BUY = TradeDecisionInput(
    action=Action.BUY_YES,
    estimated_probability=0.75,
    stake_usdc=60,
    max_acceptable_price=0.9,
    confidence=0.6,
    short_reason="ставлю на фаворита",
)

LIVE_ENV = {
    "LIVE_TRADING_ENABLED": "true",
    "EXECUTION_DRY_RUN": "false",
    "CODEX_POLY_PRIVATE_KEY": "0x" + "11" * 32,
    "CODEX_POLY_FUNDER": "0x1111111111111111111111111111111111111111",
    "CLAUDE_POLY_PRIVATE_KEY": "0x" + "22" * 32,
    "CLAUDE_POLY_FUNDER": "0x2222222222222222222222222222222222222222",
}


@pytest.fixture
def live_mode(monkeypatch):
    for key, value in LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    yield
    reset_settings_cache()


def locked_round(db: Session, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    return round_row


# --- подготовка -------------------------------------------------------------
def test_prepare_stops_before_execution(db: Session, seeded, market: Market):
    round_row = locked_round(db, market)

    proposals = rounds_service.prepare_round(db, round_row)

    assert round_row.status == RoundStatus.AWAITING_APPROVAL.value
    assert set(proposals) == {"codex", "claude", "titan"}
    for entry in proposals.values():
        assert "verdict" in entry
        assert entry["approved_by"] is None
    # ни один ордер не создан
    from app.db.models import SimulatedOrder

    assert db.query(SimulatedOrder).count() == 0


def test_prepare_shows_what_will_be_sent(db: Session, seeded, market: Market):
    round_row = locked_round(db, market)
    proposals = rounds_service.prepare_round(db, round_row)

    titan = proposals["titan"]
    assert titan["approved_stake"] > 0
    assert titan["outcome"] in ("YES", "NO")
    assert titan["expected_avg_price"] > 0
    assert titan["executable"] is True


# --- одобрение --------------------------------------------------------------
def test_approve_marks_decision(db: Session, seeded, market: Market):
    round_row = locked_round(db, market)
    rounds_service.prepare_round(db, round_row)

    decision = rounds_service.approve_decision(db, round_row, "titan", actor="nikita")

    assert decision.approved_by == "nikita"
    assert decision.approved_at is not None


def test_revoke_clears_approval(db: Session, seeded, market: Market):
    round_row = locked_round(db, market)
    rounds_service.prepare_round(db, round_row)
    rounds_service.approve_decision(db, round_row, "titan")

    decision = rounds_service.revoke_approval(db, round_row, "titan")

    assert decision.approved_by is None
    assert decision.approved_at is None


def test_approval_is_recorded_in_audit(db: Session, seeded, market: Market):
    from app.db.models import AuditEvent

    round_row = locked_round(db, market)
    rounds_service.prepare_round(db, round_row)
    rounds_service.approve_decision(db, round_row, "titan", actor="nikita")
    db.flush()

    events = [e for e in db.query(AuditEvent).all() if e.action == "approve"]
    assert events, "одобрение не попало в аудит"
    assert events[-1].actor == "nikita"


def test_cannot_approve_after_execution(db: Session, seeded, market: Market):
    round_row = locked_round(db, market)
    rounds_service.execute_round(db, round_row)

    with pytest.raises(rounds_service.RoundStateError, match="уже исполнен"):
        rounds_service.approve_decision(db, round_row, "titan")


# --- боевой режим: без одобрения деньги не двигаются -------------------------
def test_live_mode_skips_unapproved(db: Session, seeded, market: Market, live_mode):
    from app.db.models import SimulatedOrder

    round_row = locked_round(db, market)
    rounds_service.prepare_round(db, round_row)

    report = rounds_service.execute_round(db, round_row)

    for key, entry in report.items():
        if entry.get("verdict") != "REJECTED" and entry.get("requested_stake"):
            assert entry["executed"] is False, f"{key} исполнен без одобрения"
            assert "не одобрено" in entry.get("skipped", "")
    assert db.query(SimulatedOrder).count() == 0


def test_live_mode_executes_only_approved(db: Session, seeded, market: Market, live_mode):
    round_row = locked_round(db, market)
    rounds_service.prepare_round(db, round_row)
    rounds_service.approve_decision(db, round_row, "titan")

    report = rounds_service.execute_round(db, round_row)

    assert report["titan"]["executed"] is True
    for key in ("codex", "claude"):
        entry = report[key]
        if entry.get("verdict") != "REJECTED" and entry.get("requested_stake"):
            assert entry["executed"] is False


# --- бумажный режим: подтверждать нечего ------------------------------------
def test_paper_mode_executes_without_approval(db: Session, seeded, market: Market):
    """Без реальных денег кнопка одобрения не нужна — старое поведение цело."""
    round_row = locked_round(db, market)

    report = rounds_service.execute_round(db, round_row)

    assert round_row.status == RoundStatus.EXECUTED.value
    assert report["titan"]["executed"] is True


def test_execute_works_straight_after_prepare(db: Session, seeded, market: Market):
    round_row = locked_round(db, market)
    rounds_service.prepare_round(db, round_row)

    report = rounds_service.execute_round(db, round_row)

    assert round_row.status == RoundStatus.EXECUTED.value
    assert report["titan"]["executed"] is True


# --- API --------------------------------------------------------------------
def test_approval_flow_through_api(client, seeded, market: Market):
    created = client.post(
        "/api/rounds", json={"market_id": market.id, "phase": "PREMATCH"}
    ).json()
    rid = created["round_id"]
    client.post(f"/api/rounds/{rid}/request-ai")
    client.post(
        f"/api/rounds/{rid}/decisions/titan",
        json={
            "action": "BUY_YES", "estimated_probability": 0.75, "stake_usdc": 60,
            "max_acceptable_price": 0.9, "confidence": 0.6,
            "short_reason": "ставлю на фаворита",
        },
    )

    prepared = client.post(f"/api/rounds/{rid}/prepare").json()
    assert prepared["status"] == RoundStatus.AWAITING_APPROVAL.value
    assert prepared["execution_mode"] == "PAPER"
    assert prepared["approval_required"] is False
    assert "titan" in prepared["proposals"]

    approved = client.post(f"/api/rounds/{rid}/decisions/titan/approve").json()
    assert approved["approved_by"] == "operator"

    revoked = client.delete(f"/api/rounds/{rid}/decisions/titan/approve").json()
    assert revoked["approved_by"] is None


def test_net_edge_is_persisted(db: Session, seeded, market: Market):
    """edge после комиссии должен доезжать до БД, иначе панель его не покажет."""
    round_row = locked_round(db, market)
    db.flush()

    decisions = db.query(Decision).filter(Decision.round_id == round_row.id).all()
    traded = [d for d in decisions if d.action != Action.HOLD.value
              and d.status == DecisionStatus.VALID.value]
    assert traded, "не с чем сравнивать"
    for decision in traded:
        assert decision.net_edge is not None
        assert decision.taker_fee_usdc is not None
        # комиссия делает edge строго меньше сырого
        assert decision.net_edge < decision.edge
