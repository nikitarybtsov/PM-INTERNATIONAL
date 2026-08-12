"""Тесты paper execution: филлы, частичное исполнение, PnL, детерминизм, дубли."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.constants import Action, OrderStatus, Phase, PositionStatus, RiskVerdict
from app.db.models import Decision, Market, Round, Snapshot
from app.schemas.snapshot import BookLevel, MarketSnapshot, SnapshotBook, SnapshotMarketInfo
from app.services import paper_engine, settlement
from app.services import portfolio as pf
from app.services import rounds as rounds_service
from app.services.book import walk_buy, walk_sell
from app.services.risk_engine import RiskOutcome


def make_snapshot_model(market_id: int, asks, bids=None) -> MarketSnapshot:
    bids = bids or [(0.49, 1000.0)]
    return MarketSnapshot(
        phase=Phase.PREMATCH,
        market=SnapshotMarketInfo(
            market_id=market_id, external_id="m", source="mock", title="A vs B",
            team_a="A", team_b="B",
        ),
        yes_price=0.50,
        no_price=0.50,
        yes_book=SnapshotBook(
            bids=[BookLevel(price=p, size=s) for p, s in bids],
            asks=[BookLevel(price=p, size=s) for p, s in asks],
        ),
        no_book=SnapshotBook(
            bids=[BookLevel(price=0.49, size=1000)], asks=[BookLevel(price=0.51, size=1000)]
        ),
        liquidity_usdc=5000.0,
    )


def prepare(db: Session, market: Market, asks, participant_key="titan"):
    """Создать раунд, snapshot-строку и решение-заглушку для участника."""
    snapshot_model = make_snapshot_model(market.id, asks)
    snap = Snapshot(
        market_id=market.id,
        phase=Phase.PREMATCH.value,
        payload=snapshot_model.payload(),
        payload_hash=snapshot_model.content_hash(),
        captured_at=snapshot_model.captured_at,
        ttl_seconds=900,
    )
    db.add(snap)
    db.flush()
    round_row = Round(
        market_id=market.id, snapshot_id=snap.id, phase=Phase.PREMATCH.value, status="LOCKED"
    )
    db.add(round_row)
    db.flush()
    participant = rounds_service.get_participant(db, participant_key)
    decision = Decision(
        round_id=round_row.id,
        participant_id=participant.id,
        snapshot_id=snap.id,
        status="VALID",
        payload={"action": "BUY_YES", "stake_usdc": 100.0},
        action=Action.BUY_YES.value,
        stake_usdc=100.0,
        max_acceptable_price=0.99,
        estimated_probability=0.6,
    )
    db.add(decision)
    db.flush()
    return round_row, snap, snapshot_model, decision, participant


def buy_outcome(stake: float, size: float, price: float, max_price: float = 0.99) -> RiskOutcome:
    return RiskOutcome(
        verdict=RiskVerdict.APPROVED,
        approved_stake=stake,
        approved_size=size,
        outcome="YES",
        action=Action.BUY_YES,
        max_acceptable_price=max_price,
        expected_avg_price=price,
        expected_slippage_bps=0.0,
        reasons=[],
    )


# --- обход стакана ----------------------------------------------------------
def test_walk_buy_partial_when_depth_is_short():
    walk = walk_buy([BookLevel(price=0.5, size=100.0)], 100.0, 0.99)
    assert walk.filled_notional == pytest.approx(50.0)
    assert walk.limited_by_depth is True


def test_walk_buy_respects_max_price():
    levels = [BookLevel(price=0.5, size=100.0), BookLevel(price=0.7, size=100.0)]
    walk = walk_buy(levels, 100.0, 0.6)
    assert walk.filled_notional == pytest.approx(50.0)
    assert walk.limited_by_price is True


def test_walk_buy_average_price_across_levels():
    levels = [BookLevel(price=0.4, size=100.0), BookLevel(price=0.6, size=100.0)]
    walk = walk_buy(levels, 100.0, 0.99)  # 40 на первом + 60 на втором
    assert walk.avg_price == pytest.approx(0.5, abs=0.001)
    assert walk.slippage_bps > 0


def test_walk_sell_stops_at_size():
    walk = walk_sell([BookLevel(price=0.5, size=100.0)], 30.0, None)
    assert walk.filled_size == pytest.approx(30.0)
    assert walk.filled_notional == pytest.approx(15.0)


# --- исполнение -------------------------------------------------------------
def test_buy_fills_and_updates_balance_and_position(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 1000.0)])
    before = pf.get_portfolio(db, participant.id).cash_balance

    result = paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    assert result.status == OrderStatus.FILLED
    assert result.filled_size == pytest.approx(200.0)
    assert result.avg_price == pytest.approx(0.5)

    portfolio = pf.get_portfolio(db, participant.id)
    assert portfolio.cash_balance == pytest.approx(before - 100.0)
    assert portfolio.reserved_balance == pytest.approx(0.0)

    position = pf.get_position(db, participant.id, market.id, "YES")
    assert position.size == pytest.approx(200.0)
    assert position.avg_price == pytest.approx(0.5)
    assert position.cost_basis == pytest.approx(100.0)


def test_partial_fill_when_depth_insufficient(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 100.0)])
    result = paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.notional == pytest.approx(50.0)
    assert pf.get_portfolio(db, participant.id).cash_balance == pytest.approx(950.0)


def test_slippage_recorded_across_levels(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(
        db, market, [(0.40, 100.0), (0.60, 500.0)]
    )
    result = paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    assert result.order.slippage_bps > 0
    assert len(result.order.fills) == 2


def test_duplicate_execution_is_blocked(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 1000.0)])
    paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    with pytest.raises(paper_engine.DuplicateExecution):
        paper_engine.execute(
            db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
            snapshot_row=snap, snapshot_model=model,
        )


def test_execution_is_deterministic():
    seed_a = paper_engine.execution_seed(1, 2, 3, "abc")
    seed_b = paper_engine.execution_seed(1, 2, 3, "abc")
    seed_c = paper_engine.execution_seed(1, 2, 3, "abd")
    assert seed_a == seed_b
    assert seed_a != seed_c


def test_reproducible_fills_for_same_inputs():
    levels = [BookLevel(price=0.4, size=100.0), BookLevel(price=0.6, size=100.0)]
    a = walk_buy(levels, 100.0, 0.99)
    b = walk_buy(levels, 100.0, 0.99)
    assert a.avg_price == b.avg_price
    assert [(f.price, f.size) for f in a.fills] == [(f.price, f.size) for f in b.fills]


# --- продажа и расчёт -------------------------------------------------------
def test_sell_realizes_pnl(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 1000.0)])
    paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    # продаём по bid 0.60 → прибыль 0.10 × 200 = 20
    sell_model = make_snapshot_model(market.id, [(0.61, 1000.0)], bids=[(0.60, 1000.0)])
    sell_snap = Snapshot(
        market_id=market.id, phase=Phase.PREMATCH.value, payload=sell_model.payload(),
        payload_hash=sell_model.content_hash(), captured_at=sell_model.captured_at,
    )
    db.add(sell_snap)
    db.flush()
    sell_round = Round(
        market_id=market.id, snapshot_id=sell_snap.id, phase=Phase.PREMATCH.value, status="LOCKED"
    )
    db.add(sell_round)
    db.flush()
    sell_decision = Decision(
        round_id=sell_round.id, participant_id=participant.id, snapshot_id=sell_snap.id,
        status="VALID", payload={"action": "SELL"}, action=Action.SELL.value, stake_usdc=120.0,
    )
    db.add(sell_decision)
    db.flush()

    sell_risk = RiskOutcome(
        verdict=RiskVerdict.APPROVED, approved_stake=120.0, approved_size=200.0, outcome="YES",
        action=Action.SELL, max_acceptable_price=None, expected_avg_price=0.6,
        expected_slippage_bps=0.0, reasons=[],
    )
    result = paper_engine.execute(
        db, decision=sell_decision, risk=sell_risk,
        snapshot_row=sell_snap, snapshot_model=sell_model,
    )
    assert result.realized_pnl == pytest.approx(20.0)
    position = pf.get_position(db, participant.id, market.id, "YES")
    assert position.status == PositionStatus.CLOSED.value
    assert pf.get_portfolio(db, participant.id).cash_balance == pytest.approx(1020.0)


def test_settlement_pays_winner_and_zeroes_loser(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 1000.0)])
    paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    settlement.settle_market(db, market, "YES")
    # 200 контрактов × 1.00 = 200 выплата, вложено 100 → банк 1100
    assert pf.get_portfolio(db, participant.id).cash_balance == pytest.approx(1100.0)
    position = pf.get_position(db, participant.id, market.id, "YES")
    assert position.status == PositionStatus.SETTLED.value
    assert position.realized_pnl == pytest.approx(100.0)


def test_settlement_loss(db: Session, market: Market):
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 1000.0)])
    paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    settlement.settle_market(db, market, "NO")
    assert pf.get_portfolio(db, participant.id).cash_balance == pytest.approx(900.0)
    position = pf.get_position(db, participant.id, market.id, "YES")
    assert position.realized_pnl == pytest.approx(-100.0)


def test_settlement_twice_is_blocked(db: Session, market: Market):
    settlement.settle_market(db, market, "YES")
    with pytest.raises(settlement.AlreadySettled):
        settlement.settle_market(db, market, "NO")


def test_fee_is_applied(db: Session, market: Market, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("PAPER_FEE_BPS", "100")  # 1%
    reset_settings_cache()
    round_row, snap, model, decision, participant = prepare(db, market, [(0.50, 1000.0)])
    result = paper_engine.execute(
        db, decision=decision, risk=buy_outcome(100.0, 200.0, 0.5),
        snapshot_row=snap, snapshot_model=model,
    )
    assert result.fee == pytest.approx(1.0)
    assert pf.get_portfolio(db, participant.id).cash_balance == pytest.approx(899.0)
    reset_settings_cache()
