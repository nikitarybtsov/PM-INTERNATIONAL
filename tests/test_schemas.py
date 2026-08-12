"""Unit-тесты схем TradeDecision и MarketSnapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.constants import Action, Phase
from app.schemas.decision import TradeDecision, TradeDecisionInput, parse_decision_json
from app.schemas.snapshot import BookLevel, MarketSnapshot, SnapshotBook, SnapshotMarketInfo


def make_snapshot(**overrides) -> MarketSnapshot:
    base = {
        "phase": Phase.PREMATCH,
        "market": SnapshotMarketInfo(
            market_id=1, external_id="x", source="mock", title="A vs B", team_a="A", team_b="B"
        ),
        "yes_price": 0.55,
        "no_price": 0.45,
        "yes_book": SnapshotBook(
            bids=[BookLevel(price=0.54, size=100)], asks=[BookLevel(price=0.56, size=100)]
        ),
        "no_book": SnapshotBook(
            bids=[BookLevel(price=0.44, size=100)], asks=[BookLevel(price=0.46, size=100)]
        ),
        "liquidity_usdc": 5000.0,
    }
    base.update(overrides)
    return MarketSnapshot(**base)


# --- TradeDecision ----------------------------------------------------------
def test_valid_buy_decision():
    d = TradeDecisionInput(
        action=Action.BUY_YES,
        estimated_probability=0.62,
        stake_usdc=50,
        max_acceptable_price=0.6,
        confidence=0.7,
        short_reason="есть преимущество",
    )
    assert d.outcome == "YES"


def test_hold_must_have_zero_stake():
    with pytest.raises(ValidationError):
        TradeDecisionInput(
            action=Action.HOLD,
            estimated_probability=0.5,
            stake_usdc=10,
            confidence=0.5,
            short_reason="пропускаю",
        )


def test_buy_requires_max_price():
    with pytest.raises(ValidationError):
        TradeDecisionInput(
            action=Action.BUY_NO,
            estimated_probability=0.4,
            stake_usdc=10,
            confidence=0.5,
            short_reason="ставлю на NO",
        )


def test_buy_requires_positive_stake():
    with pytest.raises(ValidationError):
        TradeDecisionInput(
            action=Action.BUY_YES,
            estimated_probability=0.6,
            stake_usdc=0,
            max_acceptable_price=0.6,
            confidence=0.5,
            short_reason="ставка нулевая",
        )


@pytest.mark.parametrize("prob", [-0.1, 1.4])
def test_probability_bounds(prob):
    with pytest.raises(ValidationError):
        TradeDecisionInput(
            action=Action.HOLD,
            estimated_probability=prob,
            stake_usdc=0,
            confidence=0.5,
            short_reason="вне диапазона",
        )


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        TradeDecisionInput(
            action=Action.HOLD,
            estimated_probability=0.5,
            stake_usdc=0,
            confidence=0.5,
            short_reason="обычный ответ",
            secret_field="нельзя",
        )


def test_edge_is_computed_from_market_probability():
    inp = TradeDecisionInput(
        action=Action.BUY_YES,
        estimated_probability=0.62,
        stake_usdc=50,
        max_acceptable_price=0.6,
        confidence=0.7,
        short_reason="есть edge",
    )
    full = TradeDecision.build(
        decision=inp,
        participant_id="codex",
        snapshot_id=1,
        market_id=1,
        market_probability=0.55,
        model_name="m",
        model_version="v",
        prompt_version="v1",
    )
    assert full.edge == pytest.approx(0.07)
    assert full.participant_id == "codex"


def test_parse_decision_json_handles_code_fence():
    raw = """```json
    {"action": "HOLD", "estimated_probability": 0.5, "stake_usdc": 0,
     "confidence": 0.4, "short_reason": "жду данных"}
    ```"""
    decision = parse_decision_json(raw)
    assert decision.action == Action.HOLD


def test_parse_decision_json_rejects_garbage():
    with pytest.raises(ValueError):
        parse_decision_json("нет тут никакого json")


def test_list_fields_accept_string():
    d = TradeDecisionInput(
        action=Action.HOLD,
        estimated_probability=0.5,
        stake_usdc=0,
        confidence=0.5,
        short_reason="жду данных",
        key_factors="форма; патч",
    )
    assert d.key_factors == ["форма", "патч"]


# --- Snapshot ---------------------------------------------------------------
def test_snapshot_hash_is_stable_and_sensitive():
    moment = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    a = make_snapshot(captured_at=moment)
    b = make_snapshot(captured_at=moment)
    assert a.content_hash() == b.content_hash()
    c = make_snapshot(captured_at=moment, yes_price=0.56)
    assert c.content_hash() != a.content_hash()


def test_snapshot_staleness():
    fresh = make_snapshot(captured_at=datetime.now(UTC), ttl_seconds=900)
    assert not fresh.is_stale()
    old = make_snapshot(captured_at=datetime.now(UTC) - timedelta(seconds=1000), ttl_seconds=900)
    assert old.is_stale()


def test_snapshot_prompt_dict_contains_required_fields():
    data = make_snapshot().to_prompt_dict()
    for key in (
        "market_title",
        "teams",
        "match_start_time",
        "prices",
        "order_book",
        "best_bid_ask",
        "available_depth_usdc",
        "liquidity_usdc",
        "captured_at",
    ):
        assert key in data
