"""Тесты risk engine: лимиты, устаревший snapshot, дубли, ликвидность, проскальзывание."""

from __future__ import annotations

import pytest

from app.config import RiskLimits
from app.constants import Action, RiskVerdict
from app.schemas.snapshot import BookLevel, MarketSnapshot, SnapshotBook, SnapshotMarketInfo
from app.services.risk_engine import RiskContext, evaluate

LIMITS = RiskLimits(
    max_position_pct=0.10,
    max_match_pct=0.25,
    max_total_exposure_pct=0.50,
    min_stake_usdc=1.0,
    min_liquidity_usdc=50.0,
    max_slippage_bps=300,
    snapshot_ttl_seconds=900,
)


def snapshot(asks=None, bids=None, no_asks=None) -> MarketSnapshot:
    asks = asks if asks is not None else [(0.50, 2000.0), (0.52, 2000.0), (0.60, 2000.0)]
    bids = bids if bids is not None else [(0.49, 2000.0), (0.47, 2000.0)]
    no_asks = no_asks if no_asks is not None else [(0.50, 2000.0)]
    return MarketSnapshot(
        phase="PREMATCH",
        market=SnapshotMarketInfo(
            market_id=1, external_id="m1", source="mock", title="A vs B", team_a="A", team_b="B"
        ),
        yes_price=0.50,
        no_price=0.50,
        yes_book=SnapshotBook(
            bids=[BookLevel(price=p, size=s) for p, s in bids],
            asks=[BookLevel(price=p, size=s) for p, s in asks],
        ),
        no_book=SnapshotBook(
            bids=[BookLevel(price=0.49, size=1000)],
            asks=[BookLevel(price=p, size=s) for p, s in no_asks],
        ),
        liquidity_usdc=10_000.0,
    )


def ctx(**overrides) -> RiskContext:
    base = dict(
        action=Action.BUY_YES,
        stake_usdc=50.0,
        max_acceptable_price=0.60,
        snapshot=snapshot(),
        cash_balance=1000.0,
        available_balance=1000.0,
        total_exposure=0.0,
        market_exposure=0.0,
        limits=LIMITS,
    )
    base.update(overrides)
    return RiskContext(**base)


# --- базовые сценарии -------------------------------------------------------
def test_approves_within_limits():
    result = evaluate(ctx(stake_usdc=50.0))
    assert result.verdict == RiskVerdict.APPROVED
    assert result.approved_stake == 50.0
    assert result.outcome == "YES"


def test_hold_is_always_approved_with_zero_stake():
    result = evaluate(ctx(action=Action.HOLD, stake_usdc=0.0, max_acceptable_price=None))
    assert result.verdict == RiskVerdict.APPROVED
    assert result.approved_stake == 0.0
    assert not result.is_executable


# --- лимиты -----------------------------------------------------------------
def test_position_limit_caps_stake_at_10_percent():
    result = evaluate(ctx(stake_usdc=500.0))
    assert result.verdict == RiskVerdict.ADJUSTED
    assert result.approved_stake == 100.0  # 10% от 1000
    assert any(r.code == "max_position_pct" for r in result.reasons)


def test_match_limit_accounts_for_existing_exposure():
    # уже 240 в этом матче, лимит 25% = 250 → доступно 10
    result = evaluate(ctx(stake_usdc=100.0, market_exposure=240.0, total_exposure=240.0))
    assert result.verdict == RiskVerdict.ADJUSTED
    assert result.approved_stake == 10.0
    assert any(r.code == "max_match_pct" for r in result.reasons)


def test_total_exposure_limit():
    # 495 уже в позициях по другим рынкам, лимит 50% = 500 → доступно 5
    result = evaluate(ctx(stake_usdc=100.0, total_exposure=495.0, market_exposure=0.0))
    assert result.verdict == RiskVerdict.ADJUSTED
    assert result.approved_stake == 5.0
    assert any(r.code == "max_total_exposure_pct" for r in result.reasons)


def test_negative_balance_is_forbidden():
    result = evaluate(ctx(stake_usdc=90.0, cash_balance=1000.0, available_balance=3.0))
    assert result.approved_stake <= 3.0
    assert any(r.code == "available_balance" for r in result.reasons)


def test_reject_when_nothing_left_after_caps():
    result = evaluate(ctx(stake_usdc=50.0, available_balance=0.0))
    assert result.verdict == RiskVerdict.REJECTED
    assert result.approved_stake == 0.0
    assert any(r.code == "below_min_stake" for r in result.reasons)


# --- устаревший snapshot и дубли --------------------------------------------
def test_stale_snapshot_is_rejected():
    result = evaluate(ctx(snapshot_stale=True, stale_reason="snapshot заменён более новым"))
    assert result.verdict == RiskVerdict.REJECTED
    assert any(r.code == "stale_snapshot" for r in result.reasons)


def test_duplicate_execution_is_rejected():
    result = evaluate(ctx(already_executed=True))
    assert result.verdict == RiskVerdict.REJECTED
    assert any(r.code == "duplicate_execution" for r in result.reasons)


def test_invalid_decision_is_rejected():
    result = evaluate(ctx(decision_valid=False, decision_error="нет JSON"))
    assert result.verdict == RiskVerdict.REJECTED
    assert any(r.code == "invalid_decision" for r in result.reasons)


# --- ликвидность и цена -----------------------------------------------------
def test_min_liquidity_blocks_thin_book():
    thin = snapshot(asks=[(0.50, 20.0)])  # глубина 10 USDC < 50
    result = evaluate(ctx(snapshot=thin))
    assert result.verdict == RiskVerdict.REJECTED
    assert any(r.code == "min_liquidity" for r in result.reasons)


def test_price_above_max_acceptable_is_rejected():
    result = evaluate(ctx(max_acceptable_price=0.45))
    assert result.verdict == RiskVerdict.REJECTED
    assert any(r.code == "price_above_limit" for r in result.reasons)


def test_slippage_limit_reduces_or_rejects():
    # первый уровень крошечный, следующий сильно дороже → большое проскальзывание
    steep = snapshot(asks=[(0.50, 100.0), (0.90, 5000.0)])
    result = evaluate(ctx(stake_usdc=100.0, snapshot=steep, max_acceptable_price=0.95))
    assert result.verdict == RiskVerdict.ADJUSTED
    # заявка урезана до объёма первого уровня (0.50 × 100 = 50 USDC)
    assert result.approved_stake == pytest.approx(50.0, abs=1e-6)
    assert result.expected_slippage_bps <= LIMITS.max_slippage_bps
    assert any(r.code == "max_slippage" for r in result.reasons)


def test_partial_depth_is_flagged():
    shallow = snapshot(asks=[(0.50, 200.0)])  # 100 USDC глубины
    result = evaluate(ctx(stake_usdc=100.0, snapshot=shallow))
    codes = {r.code for r in result.reasons}
    assert "min_liquidity" not in codes


# --- SELL -------------------------------------------------------------------
def test_sell_without_position_is_rejected():
    result = evaluate(ctx(action=Action.SELL, stake_usdc=50.0, position_size=0.0))
    assert result.verdict == RiskVerdict.REJECTED
    assert any(r.code == "no_position" for r in result.reasons)


def test_sell_is_capped_by_position_size():
    result = evaluate(
        ctx(
            action=Action.SELL,
            stake_usdc=1000.0,
            position_size=10.0,
            position_avg_price=0.4,
            sell_outcome="YES",
        )
    )
    assert result.approved_size <= 10.0
    assert result.verdict in (RiskVerdict.APPROVED, RiskVerdict.ADJUSTED)


# --- журнал причин ----------------------------------------------------------
def test_every_outcome_has_reasons():
    for context in (ctx(), ctx(stake_usdc=5000.0), ctx(snapshot_stale=True)):
        result = evaluate(context)
        assert result.reasons, "risk engine обязан объяснять каждое решение"
        for reason in result.reasons:
            assert reason.code and reason.message
