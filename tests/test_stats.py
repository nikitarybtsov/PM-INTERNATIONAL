"""Тесты расчёта статистики: PnL, ROI, просадка, Brier, log loss, калибровка."""

from __future__ import annotations

import math

import pytest
from sqlalchemy.orm import Session

from app.constants import Phase
from app.db.models import Market
from app.schemas.decision import TradeDecisionInput
from app.services import rounds as rounds_service, settlement, stats
from app.services.stats import _forecast_stats, _max_drawdown

TITAN_BUY = TradeDecisionInput(
    action="BUY_YES",
    estimated_probability=0.8,
    stake_usdc=50,
    max_acceptable_price=0.99,
    confidence=0.9,
    short_reason="уверенная ставка на фаворита",
)


class FakeDecision:
    """Минимальная заглушка решения для проверки формул."""

    def __init__(self, probability: float, market_id: int, confidence: float = 0.7):
        self.status = "VALID"
        self.estimated_probability = probability
        self.confidence = confidence
        self.payload = {"market_id": market_id}


# --- чистые формулы ---------------------------------------------------------
def test_brier_and_log_loss_perfect_forecast():
    decisions = [FakeDecision(1.0, 1), FakeDecision(0.0, 2)]
    result = _forecast_stats(decisions, {1: "YES", 2: "NO"})
    assert result.brier == pytest.approx(0.0)
    assert result.log_loss == pytest.approx(0.0, abs=1e-4)
    assert result.accuracy == pytest.approx(1.0)


def test_brier_of_coin_flip():
    decisions = [FakeDecision(0.5, 1), FakeDecision(0.5, 2)]
    result = _forecast_stats(decisions, {1: "YES", 2: "NO"})
    assert result.brier == pytest.approx(0.25)
    assert result.log_loss == pytest.approx(math.log(2), abs=1e-6)


def test_worse_forecast_has_higher_brier():
    good = _forecast_stats([FakeDecision(0.9, 1)], {1: "YES"})
    bad = _forecast_stats([FakeDecision(0.2, 1)], {1: "YES"})
    assert good.brier < bad.brier
    assert good.log_loss < bad.log_loss


def test_calibration_bins():
    decisions = [FakeDecision(0.15, 1), FakeDecision(0.85, 2)]
    result = _forecast_stats(decisions, {1: "NO", 2: "YES"})
    assert len(result.calibration) == 2
    assert result.count == 2


def test_no_settled_markets_gives_empty_forecast():
    result = _forecast_stats([FakeDecision(0.7, 1)], {})
    assert result.count == 0
    assert result.brier is None


def test_max_drawdown():
    curve = [{"equity": 1000}, {"equity": 1200}, {"equity": 900}, {"equity": 1100}]
    dd, dd_pct = _max_drawdown(curve)
    assert dd == pytest.approx(300.0)
    assert dd_pct == pytest.approx(25.0)


# --- сквозной расчёт --------------------------------------------------------
def test_scoreboard_after_settlement(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    rounds_service.execute_round(db, round_row)
    settlement.settle_market(db, market, "YES")

    board = stats.scoreboard(db)
    assert len(board["participants"]) == 3
    titan = next(p for p in board["participants"] if p["participant"] == "titan")

    assert titan["initial_balance"] == 1000.0
    assert titan["bets_count"] == 1
    assert titan["equity"] > 1000.0  # ставка на YES выиграла
    assert titan["pnl_abs"] == pytest.approx(titan["equity"] - 1000.0)
    assert titan["roi"] == pytest.approx(titan["pnl_abs"] / 1000.0, abs=1e-6)
    assert titan["wins"] == 1
    assert titan["win_rate"] == pytest.approx(1.0)
    assert titan["forecast"]["brier"] is not None

    assert board["winners"]["best_trader"]["participant"] is not None
    assert board["winners"]["best_forecaster"]["participant"] is not None


def test_pnl_breakdowns_present(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    rounds_service.execute_round(db, round_row)
    settlement.settle_market(db, market, "YES")

    titan = rounds_service.get_participant(db, "titan")
    result = stats.participant_stats(db, titan)
    assert Phase.PREMATCH.value in result.pnl_by_phase
    assert result.pnl_by_market_type
    assert result.pnl_by_team
    assert len(result.equity_curve) > 1


def test_disagreements_report(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    rounds_service.execute_round(db, round_row)

    report = stats.disagreements(db)
    assert report
    assert report[0]["probability_spread"] >= 0
    assert set(report[0]["probabilities"]) == {"codex", "claude", "titan"}


def test_biggest_moves(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    rounds_service.execute_round(db, round_row)
    settlement.settle_market(db, market, "YES")

    moves = stats.biggest_moves(db)
    assert moves["biggest_wins"]
    assert moves["biggest_wins"][0]["realized_pnl"] > 0
