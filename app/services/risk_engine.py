"""Risk engine — детерминированные ограничения, одинаковые для всех участников.

Движок не знает, кто перед ним: Codex, Claude или Titan. Все проверки — чистые
функции от (решение, snapshot, состояние банка, лимиты). Каждое изменение или
отклонение заявки попадает в журнал причин.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import RiskLimits, get_settings
from app.constants import Action, RiskVerdict, money
from app.db.models import Decision, Participant, RiskEvaluation, SimulatedOrder, Snapshot
from app.schemas.snapshot import MarketSnapshot
from app.services import audit
from app.services import portfolio as pf
from app.services.book import walk_buy, walk_sell
from app.services.snapshots import is_stale


@dataclass(slots=True)
class RiskReason:
    code: str
    message: str
    severity: str = "info"  # info | adjust | reject

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass(slots=True)
class RiskContext:
    """Всё, что нужно движку. Никаких обращений к БД внутри проверок."""

    action: Action
    stake_usdc: float
    max_acceptable_price: float | None
    snapshot: MarketSnapshot
    cash_balance: float
    available_balance: float
    total_exposure: float
    market_exposure: float
    position_size: float = 0.0
    position_avg_price: float = 0.0
    sell_outcome: str = "YES"
    snapshot_stale: bool = False
    stale_reason: str | None = None
    already_executed: bool = False
    decision_valid: bool = True
    decision_error: str | None = None
    limits: RiskLimits = field(default_factory=RiskLimits)


@dataclass(slots=True)
class RiskOutcome:
    verdict: RiskVerdict
    approved_stake: float
    approved_size: float
    outcome: str | None
    action: Action
    max_acceptable_price: float | None
    expected_avg_price: float
    expected_slippage_bps: float
    reasons: list[RiskReason]

    @property
    def is_executable(self) -> bool:
        return self.verdict != RiskVerdict.REJECTED and self.action != Action.HOLD

    def reasons_payload(self) -> list[dict]:
        return [r.as_dict() for r in self.reasons]


def _reject(action: Action, reasons: list[RiskReason]) -> RiskOutcome:
    return RiskOutcome(
        verdict=RiskVerdict.REJECTED,
        approved_stake=0.0,
        approved_size=0.0,
        outcome=None,
        action=action,
        max_acceptable_price=None,
        expected_avg_price=0.0,
        expected_slippage_bps=0.0,
        reasons=reasons,
    )


def evaluate(ctx: RiskContext) -> RiskOutcome:  # noqa: C901 — линейный список проверок
    """Главная проверка. Возвращает вердикт и, при необходимости, урезанную заявку."""
    reasons: list[RiskReason] = []

    # --- блокирующие проверки ------------------------------------------------
    if not ctx.decision_valid:
        reasons.append(
            RiskReason(
                "invalid_decision",
                f"решение не прошло валидацию схемы: {ctx.decision_error or 'без деталей'}",
                "reject",
            )
        )
        return _reject(ctx.action, reasons)

    if ctx.already_executed:
        reasons.append(
            RiskReason("duplicate_execution", "заявка по этому решению уже исполнена", "reject")
        )
        return _reject(ctx.action, reasons)

    if ctx.snapshot_stale:
        reasons.append(
            RiskReason(
                "stale_snapshot",
                ctx.stale_reason or "решение принято по устаревшему snapshot",
                "reject",
            )
        )
        return _reject(ctx.action, reasons)

    if ctx.action == Action.HOLD:
        reasons.append(RiskReason("hold", "участник воздержался от сделки", "info"))
        return RiskOutcome(
            verdict=RiskVerdict.APPROVED,
            approved_stake=0.0,
            approved_size=0.0,
            outcome=None,
            action=Action.HOLD,
            max_acceptable_price=None,
            expected_avg_price=0.0,
            expected_slippage_bps=0.0,
            reasons=reasons,
        )

    if ctx.action == Action.SELL:
        return _evaluate_sell(ctx, reasons)
    return _evaluate_buy(ctx, reasons)


# ---------------------------------------------------------------------------
def _evaluate_buy(ctx: RiskContext, reasons: list[RiskReason]) -> RiskOutcome:
    limits = ctx.limits
    outcome = "YES" if ctx.action == Action.BUY_YES else "NO"
    book = ctx.snapshot.book_for(outcome)
    best_ask = book.best_ask

    if best_ask is None or not book.asks:
        reasons.append(
            RiskReason("no_liquidity", f"пустой стакан asks по исходу {outcome}", "reject")
        )
        return _reject(ctx.action, reasons)

    depth = book.ask_depth_usdc
    if depth < limits.min_liquidity_usdc:
        reasons.append(
            RiskReason(
                "min_liquidity",
                f"глубина {depth:.2f} USDC ниже минимума {limits.min_liquidity_usdc:.2f}",
                "reject",
            )
        )
        return _reject(ctx.action, reasons)

    if ctx.max_acceptable_price is not None and ctx.max_acceptable_price < best_ask:
        reasons.append(
            RiskReason(
                "price_above_limit",
                f"лучший ask {best_ask:.4f} выше max_acceptable_price "
                f"{ctx.max_acceptable_price:.4f}",
                "reject",
            )
        )
        return _reject(ctx.action, reasons)

    stake = money(ctx.stake_usdc)
    requested = stake

    # --- лимиты банка --------------------------------------------------------
    caps: list[tuple[str, float, str]] = [
        (
            "max_position_pct",
            money(ctx.cash_balance * limits.max_position_pct),
            f"не более {limits.max_position_pct:.0%} банка на одну позицию",
        ),
        (
            "max_match_pct",
            money(max(0.0, ctx.cash_balance * limits.max_match_pct - ctx.market_exposure)),
            f"не более {limits.max_match_pct:.0%} банка на один матч",
        ),
        (
            "max_total_exposure_pct",
            money(max(0.0, ctx.cash_balance * limits.max_total_exposure_pct - ctx.total_exposure)),
            f"не более {limits.max_total_exposure_pct:.0%} банка в открытых позициях",
        ),
        ("available_balance", money(ctx.available_balance), "запрет отрицательного баланса"),
    ]
    for code, cap, description in caps:
        if stake > cap:
            reasons.append(
                RiskReason(
                    code,
                    f"{description}: заявка {stake:.2f} → {max(cap, 0.0):.2f} USDC",
                    "adjust" if cap > 0 else "reject",
                )
            )
            stake = money(max(cap, 0.0))

    if stake < limits.min_stake_usdc:
        reasons.append(
            RiskReason(
                "below_min_stake",
                f"после ограничений остаётся {stake:.2f} USDC — меньше минимума "
                f"{limits.min_stake_usdc:.2f}",
                "reject",
            )
        )
        return _reject(ctx.action, reasons)

    # --- проскальзывание -----------------------------------------------------
    walk = walk_buy(book.asks, stake, ctx.max_acceptable_price)
    if walk.is_empty:
        reasons.append(
            RiskReason("no_fill", "по заданной цене стакан не даёт исполнения", "reject")
        )
        return _reject(ctx.action, reasons)

    if walk.slippage_bps > limits.max_slippage_bps:
        # пробуем урезать до объёма первого уровня
        first = book.asks[0]
        reduced = money(min(stake, first.price * first.size))
        if reduced < limits.min_stake_usdc:
            reasons.append(
                RiskReason(
                    "max_slippage",
                    f"ожидаемое проскальзывание {walk.slippage_bps:.0f} б.п. выше лимита "
                    f"{limits.max_slippage_bps} б.п., урезать до допустимого нельзя",
                    "reject",
                )
            )
            return _reject(ctx.action, reasons)
        reasons.append(
            RiskReason(
                "max_slippage",
                f"проскальзывание {walk.slippage_bps:.0f} б.п. выше лимита "
                f"{limits.max_slippage_bps} б.п.: заявка урезана {stake:.2f} → {reduced:.2f} USDC",
                "adjust",
            )
        )
        stake = reduced
        walk = walk_buy(book.asks, stake, ctx.max_acceptable_price)

    if walk.limited_by_depth:
        reasons.append(
            RiskReason(
                "partial_depth",
                f"глубины хватает лишь на {walk.filled_notional:.2f} из {stake:.2f} USDC — "
                "ожидается частичное исполнение",
                "info",
            )
        )

    verdict = RiskVerdict.APPROVED if stake == requested else RiskVerdict.ADJUSTED
    if verdict == RiskVerdict.APPROVED:
        reasons.append(RiskReason("approved", "заявка прошла все лимиты без изменений", "info"))

    return RiskOutcome(
        verdict=verdict,
        approved_stake=stake,
        approved_size=walk.filled_size,
        outcome=outcome,
        action=ctx.action,
        max_acceptable_price=ctx.max_acceptable_price,
        expected_avg_price=walk.avg_price,
        expected_slippage_bps=walk.slippage_bps,
        reasons=reasons,
    )


def _evaluate_sell(ctx: RiskContext, reasons: list[RiskReason]) -> RiskOutcome:
    limits = ctx.limits
    if ctx.position_size <= 0:
        reasons.append(
            RiskReason("no_position", "нет открытой позиции для продажи", "reject")
        )
        return _reject(ctx.action, reasons)

    outcome = ctx.sell_outcome or "YES"
    book = ctx.snapshot.book_for(outcome)
    if not book.bids:
        reasons.append(RiskReason("no_liquidity", "пустой стакан bids для продажи", "reject"))
        return _reject(ctx.action, reasons)

    # stake_usdc для SELL трактуется как сумма выручки, которую хочет получить участник
    best_bid = book.best_bid or 0.0
    want_size = ctx.stake_usdc / best_bid if best_bid > 0 else 0.0
    size_to_sell = min(want_size, ctx.position_size)
    if size_to_sell < ctx.position_size and want_size > ctx.position_size:
        reasons.append(
            RiskReason(
                "position_cap",
                f"продажа урезана до размера позиции {ctx.position_size:.4f} контрактов",
                "adjust",
            )
        )

    walk = walk_sell(book.bids, size_to_sell, None)
    if walk.is_empty:
        reasons.append(RiskReason("no_fill", "стакан bids не даёт исполнения", "reject"))
        return _reject(ctx.action, reasons)

    if walk.slippage_bps > limits.max_slippage_bps:
        reasons.append(
            RiskReason(
                "max_slippage",
                f"проскальзывание при продаже {walk.slippage_bps:.0f} б.п. выше лимита "
                f"{limits.max_slippage_bps} б.п.",
                "reject",
            )
        )
        return _reject(ctx.action, reasons)

    verdict = (
        RiskVerdict.ADJUSTED
        if any(r.severity == "adjust" for r in reasons) or walk.limited_by_depth
        else RiskVerdict.APPROVED
    )
    if walk.limited_by_depth:
        reasons.append(
            RiskReason(
                "partial_depth",
                f"стакан позволяет продать {walk.filled_size:.4f} из {size_to_sell:.4f}",
                "info",
            )
        )
    return RiskOutcome(
        verdict=verdict,
        approved_stake=money(walk.filled_notional),
        approved_size=walk.filled_size,
        outcome=outcome,
        action=Action.SELL,
        max_acceptable_price=None,
        expected_avg_price=walk.avg_price,
        expected_slippage_bps=walk.slippage_bps,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Интеграция с БД
# ---------------------------------------------------------------------------
def build_context(
    db: Session,
    decision: Decision,
    snapshot_row: Snapshot,
    snapshot_model: MarketSnapshot,
    participant: Participant,
) -> RiskContext:
    portfolio = pf.get_portfolio(db, participant.id)
    stale, stale_reason = is_stale(snapshot_row)

    action_value = decision.action or Action.HOLD.value
    action = Action(action_value)

    # для SELL определяем, какую позицию закрываем: берём наибольшую по рынку
    position_size = 0.0
    position_avg = 0.0
    sell_outcome = "YES"
    if action == Action.SELL:
        positions = pf.open_positions(db, participant.id, snapshot_row.market_id)
        if positions:
            biggest = max(positions, key=lambda p: p.size)
            position_size, position_avg = biggest.size, biggest.avg_price
            sell_outcome = biggest.outcome

    already = (
        db.query(SimulatedOrder.id).filter(SimulatedOrder.decision_id == decision.id).first()
        is not None
    )

    ctx = RiskContext(
        action=action,
        stake_usdc=decision.stake_usdc or 0.0,
        max_acceptable_price=decision.max_acceptable_price,
        snapshot=snapshot_model,
        cash_balance=money(portfolio.cash_balance),
        available_balance=money(portfolio.available_balance),
        total_exposure=pf.exposure(db, participant.id),
        market_exposure=pf.exposure(db, participant.id, snapshot_row.market_id),
        position_size=position_size,
        position_avg_price=position_avg,
        sell_outcome=sell_outcome,
        snapshot_stale=stale,
        stale_reason=stale_reason,
        already_executed=already,
        decision_valid=decision.status == "VALID",
        decision_error=decision.validation_error,
        limits=get_settings().risk,
    )
    return ctx


def persist_evaluation(
    db: Session, decision: Decision, ctx: RiskContext, result: RiskOutcome
) -> RiskEvaluation:
    before = dict(decision.payload)
    after = dict(before)
    after.update(
        {
            "action": result.action.value,
            "stake_usdc": result.approved_stake,
            "max_acceptable_price": result.max_acceptable_price,
            "risk_verdict": result.verdict.value,
            "expected_avg_price": result.expected_avg_price,
            "expected_slippage_bps": result.expected_slippage_bps,
        }
    )
    # Оценка на решение одна: risk engine прогоняется и при подготовке раунда,
    # и при исполнении, а между ними оператор может обновить подготовку. Поэтому
    # запись обновляется, а не дублируется — иначе UNIQUE-ограничение падает.
    # Каждая переоценка всё равно попадает в аудит отдельным событием.
    evaluation = db.scalar(
        select(RiskEvaluation).where(RiskEvaluation.decision_id == decision.id)
    )
    if evaluation is None:
        evaluation = RiskEvaluation(decision_id=decision.id)
        db.add(evaluation)

    evaluation.verdict = result.verdict.value
    evaluation.decision_before = before
    evaluation.decision_after = after
    evaluation.requested_stake = money(ctx.stake_usdc)
    evaluation.approved_stake = result.approved_stake
    evaluation.reasons = result.reasons_payload()
    evaluation.limits_snapshot = ctx.limits.model_dump()
    db.flush()
    audit.record(
        db,
        entity_type="risk_evaluation",
        entity_id=evaluation.id,
        action=f"verdict:{result.verdict.value}",
        before={"stake_usdc": ctx.stake_usdc, "action": ctx.action.value},
        after={"stake_usdc": result.approved_stake, "action": result.action.value},
        note="; ".join(r.message for r in result.reasons)[:2000],
    )
    return evaluation
