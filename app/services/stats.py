"""Статистика эксперимента.

Считается отдельно по каждому участнику. Два независимых зачёта:
  * лучший ТРЕЙДЕР      — по итоговому банку (equity);
  * лучший ПРОГНОЗИСТ   — по качеству вероятностных прогнозов (Brier / log loss).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import Action, DecisionStatus, PositionStatus, money
from app.db.models import (
    Decision,
    LedgerEntry,
    Market,
    Participant,
    Position,
    Round,
    Settlement,
    SimulatedOrder,
)
from app.services import paper_engine
from app.services import portfolio as pf

EPS = 1e-9
CLAMP = 1e-6


@dataclass
class ForecastStats:
    count: int = 0
    brier: float | None = None
    log_loss: float | None = None
    calibration: list[dict] = field(default_factory=list)
    mean_confidence: float | None = None
    accuracy: float | None = None


@dataclass
class ParticipantStats:
    participant: str
    display_name: str
    initial_balance: float
    cash_balance: float
    open_positions_value: float
    equity: float
    pnl_abs: float
    pnl_pct: float
    roi: float
    realized_pnl: float
    unrealized_pnl: float
    bets_count: int
    hold_count: int
    invalid_count: int
    avg_stake: float
    total_staked: float
    win_rate: float | None
    wins: int
    losses: int
    max_drawdown: float
    max_drawdown_pct: float
    forecast: ForecastStats
    pnl_by_phase: dict[str, float]
    pnl_by_market_type: dict[str, float]
    pnl_by_team: dict[str, float]
    equity_curve: list[dict]

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["forecast"] = self.forecast.__dict__
        return data


# ---------------------------------------------------------------------------
def _settled_outcomes(db: Session) -> dict[int, str]:
    return {s.market_id: s.winning_outcome for s in db.scalars(select(Settlement))}


def _equity_curve(db: Session, participant_id: int, initial: float) -> list[dict]:
    entries = list(
        db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.participant_id == participant_id)
            .order_by(LedgerEntry.id)
        )
    )
    curve = [{"t": None, "equity": money(initial), "event": "start"}]
    for e in entries:
        curve.append(
            {
                "t": e.created_at.isoformat() if e.created_at else None,
                "equity": money(e.equity_after),
                "event": e.entry_type,
            }
        )
    return curve


def _max_drawdown(curve: list[dict]) -> tuple[float, float]:
    peak = None
    max_dd = 0.0
    max_dd_pct = 0.0
    for point in curve:
        value = point["equity"]
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            dd = peak - value
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd / peak * 100.0
    return money(max_dd), round(max_dd_pct, 2)


def _forecast_stats(decisions: list[Decision], outcomes: dict[int, str]) -> ForecastStats:
    """Brier / log loss / калибровка по прогнозам вероятности YES."""
    pairs: list[tuple[float, int, float | None]] = []
    for d in decisions:
        if d.status != DecisionStatus.VALID.value or d.estimated_probability is None:
            continue
        winner = outcomes.get(d.payload.get("market_id") if d.payload else None)
        if winner is None:
            continue
        pairs.append((float(d.estimated_probability), 1 if winner == "YES" else 0, d.confidence))

    if not pairs:
        return ForecastStats()

    brier = sum((p - y) ** 2 for p, y, _ in pairs) / len(pairs)
    log_loss = -sum(
        y * math.log(min(max(p, CLAMP), 1 - CLAMP))
        + (1 - y) * math.log(1 - min(max(p, CLAMP), 1 - CLAMP))
        for p, y, _ in pairs
    ) / len(pairs)
    accuracy = sum(1 for p, y, _ in pairs if (p >= 0.5) == (y == 1)) / len(pairs)
    confidences = [c for _, _, c in pairs if c is not None]

    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, y, _ in pairs:
        bins[min(int(p * 10), 9)].append((p, y))
    calibration = [
        {
            "bin": f"{i / 10:.1f}–{(i + 1) / 10:.1f}",
            "count": len(items),
            "mean_predicted": round(sum(p for p, _ in items) / len(items), 4),
            "observed_rate": round(sum(y for _, y in items) / len(items), 4),
        }
        for i, items in sorted(bins.items())
    ]

    return ForecastStats(
        count=len(pairs),
        brier=round(brier, 6),
        log_loss=round(log_loss, 6),
        calibration=calibration,
        mean_confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        accuracy=round(accuracy, 4),
    )


def _position_pnl_breakdown(
    db: Session, participant_id: int, marks: dict[tuple[int, str], float]
) -> tuple[dict[str, float], dict[str, float], dict[str, float], int, int]:
    """PnL по фазам / типам рынка / командам + счётчик выигранных и проигранных ставок."""
    by_phase: dict[str, float] = defaultdict(float)
    by_type: dict[str, float] = defaultdict(float)
    by_team: dict[str, float] = defaultdict(float)
    wins = losses = 0

    positions = list(
        db.scalars(select(Position).where(Position.participant_id == participant_id))
    )
    for pos in positions:
        market = db.get(Market, pos.market_id)
        pnl = pos.realized_pnl
        if pos.status == PositionStatus.OPEN.value:
            mark = marks.get((pos.market_id, pos.outcome), pos.avg_price)
            pnl = money(pnl + pos.size * mark - pos.cost_basis)
        else:
            if pos.realized_pnl > EPS:
                wins += 1
            elif pos.realized_pnl < -EPS:
                losses += 1

        by_phase[pos.phase_opened or "UNKNOWN"] += pnl
        if market:
            by_type[market.market_type or "UNKNOWN"] += pnl
            team = (
                (market.team_a or market.yes_label)
                if pos.outcome == "YES"
                else (market.team_b or market.no_label)
            )
            by_team[team or "UNKNOWN"] += pnl

    return (
        {k: money(v) for k, v in by_phase.items()},
        {k: money(v) for k, v in by_type.items()},
        {k: money(v) for k, v in by_team.items()},
        wins,
        losses,
    )


def participant_stats(db: Session, participant: Participant) -> ParticipantStats:
    portfolio = pf.get_portfolio(db, participant.id)
    marks = paper_engine.latest_marks(db)
    outcomes = _settled_outcomes(db)

    open_pos = pf.open_positions(db, participant.id)
    open_value = money(
        sum(p.size * marks.get((p.market_id, p.outcome), p.avg_price) for p in open_pos)
    )
    equity = money(portfolio.cash_balance + open_value)
    initial = portfolio.initial_balance

    realized = money(
        sum(
            p.realized_pnl
            for p in db.scalars(select(Position).where(Position.participant_id == participant.id))
        )
    )
    unrealized = paper_engine.unrealized_pnl(db, participant.id, marks)

    decisions = list(
        db.scalars(select(Decision).where(Decision.participant_id == participant.id))
    )
    bets = [d for d in decisions if d.action and d.action != Action.HOLD.value]
    holds = [d for d in decisions if d.action == Action.HOLD.value]
    invalid = [d for d in decisions if d.status != DecisionStatus.VALID.value]

    orders = list(
        db.scalars(
            select(SimulatedOrder).where(SimulatedOrder.participant_id == participant.id)
        )
    )
    total_staked = money(sum(o.notional for o in orders))
    filled_orders = [o for o in orders if o.filled_size > 0]

    by_phase, by_type, by_team, wins, losses = _position_pnl_breakdown(db, participant.id, marks)

    curve = _equity_curve(db, participant.id, initial)
    max_dd, max_dd_pct = _max_drawdown(curve)

    settled_count = wins + losses
    return ParticipantStats(
        participant=participant.key,
        display_name=participant.display_name,
        initial_balance=money(initial),
        cash_balance=money(portfolio.cash_balance),
        open_positions_value=open_value,
        equity=equity,
        pnl_abs=money(equity - initial),
        pnl_pct=round((equity - initial) / initial * 100.0, 2) if initial else 0.0,
        roi=round((equity - initial) / initial, 6) if initial else 0.0,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        bets_count=len(bets),
        hold_count=len(holds),
        invalid_count=len(invalid),
        avg_stake=money(total_staked / len(filled_orders)) if filled_orders else 0.0,
        total_staked=total_staked,
        win_rate=round(wins / settled_count, 4) if settled_count else None,
        wins=wins,
        losses=losses,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        forecast=_forecast_stats(decisions, outcomes),
        pnl_by_phase=by_phase,
        pnl_by_market_type=by_type,
        pnl_by_team=by_team,
        equity_curve=curve,
    )


def scoreboard(db: Session) -> dict:
    """Общая таблица и два победителя."""
    participants = list(db.scalars(select(Participant).order_by(Participant.id)))
    stats = [participant_stats(db, p) for p in participants]

    best_trader = max(stats, key=lambda s: s.equity, default=None)
    scored = [s for s in stats if s.forecast.brier is not None]
    best_forecaster = min(scored, key=lambda s: s.forecast.brier) if scored else None

    return {
        "participants": [s.to_dict() for s in stats],
        "winners": {
            "best_trader": {
                "participant": best_trader.participant if best_trader else None,
                "equity": best_trader.equity if best_trader else None,
                "metric": "итоговый банк (equity)",
            },
            "best_forecaster": {
                "participant": best_forecaster.participant if best_forecaster else None,
                "brier": best_forecaster.forecast.brier if best_forecaster else None,
                "log_loss": best_forecaster.forecast.log_loss if best_forecaster else None,
                "metric": "Brier score (меньше — лучше)",
                "note": None if best_forecaster else "нет рассчитанных рынков для оценки прогнозов",
            },
        },
        "rounds_total": db.query(Round.id).count(),
        "markets_settled": len(_settled_outcomes(db)),
    }


def biggest_moves(db: Session, limit: int = 5) -> dict:
    """Самые крупные выигрыши и проигрыши — материал для монтажа."""
    rows = []
    for pos in db.scalars(select(Position).where(Position.status != PositionStatus.OPEN.value)):
        market = db.get(Market, pos.market_id)
        participant = db.get(Participant, pos.participant_id)
        rows.append(
            {
                "participant": participant.key if participant else "?",
                "market": market.title if market else "?",
                "outcome": pos.outcome,
                "realized_pnl": money(pos.realized_pnl),
                "phase": pos.phase_opened,
            }
        )
    rows.sort(key=lambda r: r["realized_pnl"], reverse=True)
    return {"biggest_wins": rows[:limit], "biggest_losses": list(reversed(rows[-limit:]))}


def disagreements(db: Session, limit: int = 10) -> list[dict]:
    """Раунды, где оценки участников разошлись сильнее всего."""
    result = []
    for round_row in db.scalars(select(Round).order_by(Round.id)):
        decisions = list(db.scalars(select(Decision).where(Decision.round_id == round_row.id)))
        probs = {}
        actions = {}
        for d in decisions:
            participant = db.get(Participant, d.participant_id)
            if participant is None or d.estimated_probability is None:
                continue
            probs[participant.key] = round(d.estimated_probability, 4)
            actions[participant.key] = d.action
        if len(probs) < 2:
            continue
        spread = round(max(probs.values()) - min(probs.values()), 4)
        market = db.get(Market, round_row.market_id)
        result.append(
            {
                "round_id": round_row.id,
                "market": market.title if market else "?",
                "phase": round_row.phase,
                "probability_spread": spread,
                "probabilities": probs,
                "actions": actions,
            }
        )
    result.sort(key=lambda r: r["probability_spread"], reverse=True)
    return result[:limit]
