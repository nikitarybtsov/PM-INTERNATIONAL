"""Комиссия Polymarket: формула, порог безубыточности, влияние на edge.

Эталонные значения взяты с живого feeSchedule рынков The International 2026:
{"rate": 0.05, "exponent": 1, "rebateRate": 0.15, "takerOnly": true}.
"""

from __future__ import annotations

import pytest

from app.services.fees import (
    FeeSchedule,
    breakeven_probability,
    expected_value,
    fee_per_usdc,
    net_edge,
    taker_fee,
)


def test_fee_matches_official_formula():
    """fee = C × rate × p × (1 − p)."""
    # 100 контрактов по 0.70: 100 × 0.05 × 0.70 × 0.30
    assert taker_fee(100, 0.70) == pytest.approx(1.05)
    # 100 по 0.31
    assert taker_fee(100, 0.31) == pytest.approx(100 * 0.05 * 0.31 * 0.69)


def test_fee_is_symmetric_and_peaks_at_half():
    """Множитель p·(1−p) симметричен относительно 0.5 и там же максимален."""
    assert taker_fee(100, 0.30) == pytest.approx(taker_fee(100, 0.70))
    assert taker_fee(100, 0.50) > taker_fee(100, 0.70)
    assert taker_fee(100, 0.50) > taker_fee(100, 0.30)


def test_fee_differs_from_arb_scan_stake_formula():
    """В arb_scan_stake комиссия считается как C × rate × (1 − p), без множителя p.

    Та формула завышает комиссию в 1/p раз — тест фиксирует, что здесь она
    официальная, чтобы расхождение не переехало сюда копированием.
    """
    size, price, rate = 100, 0.31, 0.05
    legacy = size * rate * (1 - price)
    official = taker_fee(size, price)
    assert official == pytest.approx(size * rate * price * (1 - price))
    assert legacy / official == pytest.approx(1 / price, rel=1e-6)


def test_rebate_reduces_fee():
    full = taker_fee(100, 0.70)
    with_rebate = taker_fee(100, 0.70, FeeSchedule(rate=0.05, rebate_rate=0.18))
    assert with_rebate == pytest.approx(full * 0.82)


def test_rebate_is_zero_by_default():
    """Банк $1 000 не дотягивает до Bronze — по умолчанию возврата нет."""
    assert FeeSchedule().rebate_rate == 0.0


def test_fee_per_usdc_is_rate_times_complement():
    """В долях от вложенного: 0.05 × (1 − p)."""
    assert fee_per_usdc(0.70) == pytest.approx(0.05 * 0.30)
    assert fee_per_usdc(0.50) == pytest.approx(0.05 * 0.50)


def test_breakeven_probability_above_price():
    """Купить по 0.70 выгодно только если вероятность выше 0.7105."""
    assert breakeven_probability(0.70) == pytest.approx(0.7105)
    assert breakeven_probability(0.50) == pytest.approx(0.5125)


def test_net_edge_can_flip_a_positive_looking_bet():
    """Ставка с «преимуществом» 0.8 п.п. на деле убыточна после комиссии."""
    price, estimate = 0.70, 0.708
    raw_edge = estimate - price
    assert raw_edge > 0
    assert net_edge(estimate, price) < 0


def test_net_edge_positive_when_estimate_clears_threshold():
    assert net_edge(0.75, 0.70) == pytest.approx(0.0395)


def test_expected_value_scales_with_size():
    ev_100 = expected_value(100, 0.75, 0.70)
    ev_200 = expected_value(200, 0.75, 0.70)
    assert ev_200 == pytest.approx(ev_100 * 2)
    assert ev_100 == pytest.approx(100 * 0.0395)


def test_zero_and_boundary_inputs_are_safe():
    assert taker_fee(0, 0.5) == 0.0
    assert taker_fee(100, 0.0) == 0.0
    assert taker_fee(100, 1.0) == 0.0
    assert taker_fee(-5, 0.5) == 0.0


def test_schedule_parsed_from_live_market_payload():
    payload = {"rate": 0.05, "exponent": 1, "rebateRate": 0.15, "takerOnly": True}
    schedule = FeeSchedule.from_market_payload(payload)
    assert schedule.rate == pytest.approx(0.05)
    assert schedule.exponent == 1
    assert schedule.taker_only is True
    # rebateRate рынка — доля мейкеров, наш тир берётся из конфига
    assert schedule.rebate_rate == 0.0


def test_schedule_falls_back_on_garbage():
    for payload in (None, "нет", {}, {"rate": "abc"}):
        schedule = FeeSchedule.from_market_payload(payload)
        assert schedule.rate == pytest.approx(0.05)
        assert schedule.exponent == 1
