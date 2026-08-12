"""Paper execution engine — симуляция исполнения. Реальных ордеров нет.

Свойства симуляции:
  * исполнение по стакану из snapshot (ask для покупки, bid для продажи);
  * частичное исполнение при нехватке глубины;
  * проскальзывание = разница средней цены и лучшей котировки;
  * комиссия PAPER_FEE_BPS от оборота;
  * резервирование средств на время исполнения;
  * позиции YES/NO с средней ценой и realized PnL;
  * защита от повторного исполнения одного решения (UNIQUE decision_id);
  * детерминизм: результат зависит только от snapshot и заявки, seed фиксируется.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import LIVE_TRADING_ENABLED, get_settings
from app.constants import (
    Action,
    LedgerType,
    OrderStatus,
    PositionStatus,
    money,
)
from app.constants import (
    price as round_price,
)
from app.constants import (
    size as round_size,
)
from app.db.models import Decision, Fill, Market, Position, SimulatedOrder, Snapshot
from app.schemas.snapshot import MarketSnapshot
from app.services import audit
from app.services import portfolio as pf
from app.services.book import WalkResult, walk_buy, walk_sell
from app.services.risk_engine import RiskOutcome

logger = logging.getLogger(__name__)


class DuplicateExecution(RuntimeError):
    """Попытка исполнить одно и то же решение дважды."""


class LiveTradingForbidden(RuntimeError):
    """Страховка: живое исполнение в этом проекте отсутствует."""


@dataclass(slots=True)
class ExecutionResult:
    order: SimulatedOrder
    filled_size: float
    avg_price: float
    notional: float
    fee: float
    status: OrderStatus
    realized_pnl: float = 0.0


def execution_seed(round_id: int, participant_id: int, market_id: int, snapshot_hash: str) -> str:
    """Детерминированный seed — одинаковые входные данные дают одинаковый результат."""
    raw = f"{round_id}:{participant_id}:{market_id}:{snapshot_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _fee_for(notional: float) -> float:
    bps = get_settings().paper_fee_bps
    return money(notional * bps / 10_000) if bps else 0.0


def execute(
    db: Session,
    *,
    decision: Decision,
    risk: RiskOutcome,
    snapshot_row: Snapshot,
    snapshot_model: MarketSnapshot,
) -> ExecutionResult | None:
    """Исполнить одобренную риск-движком заявку в бумажном режиме."""
    if LIVE_TRADING_ENABLED:  # pragma: no cover — константа всегда False
        raise LiveTradingForbidden("live execution отключён на уровне кода")

    if not risk.is_executable:
        return None

    existing = db.scalar(select(SimulatedOrder).where(SimulatedOrder.decision_id == decision.id))
    if existing is not None:
        raise DuplicateExecution(
            f"решение #{decision.id} уже исполнено ордером #{existing.id}"
        )

    market_id = snapshot_row.market_id
    participant_id = decision.participant_id
    seed = execution_seed(decision.round_id, participant_id, market_id, snapshot_row.payload_hash)

    order = SimulatedOrder(
        decision_id=decision.id,
        round_id=decision.round_id,
        participant_id=participant_id,
        market_id=market_id,
        snapshot_id=snapshot_row.id,
        action=risk.action.value,
        outcome=risk.outcome or "YES",
        limit_price=risk.max_acceptable_price or 0.0,
        requested_notional=risk.approved_stake,
        requested_size=risk.approved_size,
        seed=seed,
        status=OrderStatus.PENDING.value,
    )
    db.add(order)
    db.flush()

    if risk.action in (Action.BUY_YES, Action.BUY_NO):
        result = _execute_buy(db, order, risk, snapshot_model)
    else:
        result = _execute_sell(db, order, risk, snapshot_model)

    audit.record(
        db,
        entity_type="simulated_order",
        entity_id=order.id,
        action=f"execute:{order.status}",
        after={
            "action": order.action,
            "outcome": order.outcome,
            "filled_size": order.filled_size,
            "avg_price": order.avg_fill_price,
            "notional": order.notional,
            "fee": order.fee,
            "seed": seed,
        },
    )
    return result


# ---------------------------------------------------------------------------
def _write_fills(db: Session, order: SimulatedOrder, walk: WalkResult, total_fee: float) -> None:
    if not walk.fills:
        return
    fee_per_notional = total_fee / walk.filled_notional if walk.filled_notional else 0.0
    for lf in walk.fills:
        db.add(
            Fill(
                order_id=order.id,
                level_index=lf.level_index,
                size=lf.size,
                price=lf.price,
                notional=money(lf.notional),
                fee=money(lf.notional * fee_per_notional),
            )
        )
    db.flush()


def _execute_buy(
    db: Session, order: SimulatedOrder, risk: RiskOutcome, snapshot: MarketSnapshot
) -> ExecutionResult:
    outcome = risk.outcome or "YES"
    book = snapshot.book_for(outcome)
    budget = risk.approved_stake

    # резервируем средства на время исполнения
    pf.reserve(db, order.participant_id, budget)
    pf.add_ledger(
        db,
        participant_id=order.participant_id,
        entry_type=LedgerType.RESERVE,
        amount=-budget,
        ref_type="order",
        ref_id=order.id,
        note="резерв под заявку",
    )

    walk = walk_buy(book.asks, budget, risk.max_acceptable_price)
    fee = _fee_for(walk.filled_notional)
    spent = money(walk.filled_notional + fee)

    # снимаем резерв и списываем фактическую сумму
    pf.release(db, order.participant_id, budget)
    if walk.is_empty:
        order.status = OrderStatus.REJECTED.value
        order.reject_reason = "стакан не дал исполнения по заданной цене"
        db.flush()
        pf.add_ledger(
            db,
            participant_id=order.participant_id,
            entry_type=LedgerType.RELEASE,
            amount=budget,
            ref_type="order",
            ref_id=order.id,
            note="возврат резерва: исполнения не было",
        )
        return ExecutionResult(order, 0.0, 0.0, 0.0, 0.0, OrderStatus.REJECTED)

    pf.debit(db, order.participant_id, spent)
    pf.add_ledger(
        db,
        participant_id=order.participant_id,
        entry_type=LedgerType.TRADE_COST,
        amount=-spent,
        ref_type="order",
        ref_id=order.id,
        note=f"покупка {outcome} {walk.filled_size:.4f} @ {walk.avg_price:.4f}",
    )
    if fee:
        pf.add_ledger(
            db,
            participant_id=order.participant_id,
            entry_type=LedgerType.FEE,
            amount=-fee,
            ref_type="order",
            ref_id=order.id,
            note="комиссия",
        )

    order.filled_size = walk.filled_size
    order.avg_fill_price = walk.avg_price
    order.notional = money(walk.filled_notional)
    order.fee = fee
    order.slippage_bps = walk.slippage_bps
    partial = walk.filled_notional + 1e-6 < budget
    order.status = (
        OrderStatus.PARTIALLY_FILLED.value if partial else OrderStatus.FILLED.value
    )
    db.flush()
    _write_fills(db, order, walk, fee)

    _apply_buy_to_position(db, order, walk, fee)
    return ExecutionResult(
        order,
        walk.filled_size,
        walk.avg_price,
        money(walk.filled_notional),
        fee,
        OrderStatus(order.status),
    )


def _apply_buy_to_position(
    db: Session, order: SimulatedOrder, walk: WalkResult, fee: float
) -> None:
    position = pf.get_position(db, order.participant_id, order.market_id, order.outcome)
    if position is None:
        position = Position(
            participant_id=order.participant_id,
            market_id=order.market_id,
            outcome=order.outcome,
            opened_round_id=order.round_id,
        )
        db.add(position)
        db.flush()

    new_size = round_size(position.size + walk.filled_size)
    new_cost = money(position.cost_basis + walk.filled_notional)
    position.size = new_size
    position.cost_basis = new_cost
    position.avg_price = round_price(new_cost / new_size) if new_size > 0 else 0.0
    position.fees_paid = money(position.fees_paid + fee)
    position.status = PositionStatus.OPEN.value
    if position.phase_opened is None:
        snapshot = db.get(Snapshot, order.snapshot_id)
        position.phase_opened = snapshot.phase if snapshot else None
    db.flush()


def _execute_sell(
    db: Session, order: SimulatedOrder, risk: RiskOutcome, snapshot: MarketSnapshot
) -> ExecutionResult:
    outcome = risk.outcome or "YES"
    position = pf.get_position(db, order.participant_id, order.market_id, outcome)
    if position is None or position.size <= 0:
        order.status = OrderStatus.REJECTED.value
        order.reject_reason = "нет позиции для продажи"
        db.flush()
        return ExecutionResult(order, 0.0, 0.0, 0.0, 0.0, OrderStatus.REJECTED)

    book = snapshot.book_for(outcome)
    size_to_sell = round_size(min(risk.approved_size, position.size))
    walk = walk_sell(book.bids, size_to_sell, None)
    if walk.is_empty:
        order.status = OrderStatus.REJECTED.value
        order.reject_reason = "стакан bids не дал исполнения"
        db.flush()
        return ExecutionResult(order, 0.0, 0.0, 0.0, 0.0, OrderStatus.REJECTED)

    fee = _fee_for(walk.filled_notional)
    proceeds = money(walk.filled_notional - fee)

    cost_of_sold = money(position.avg_price * walk.filled_size)
    realized = money(walk.filled_notional - cost_of_sold - fee)

    position.size = round_size(position.size - walk.filled_size)
    position.cost_basis = money(max(0.0, position.cost_basis - cost_of_sold))
    position.realized_pnl = money(position.realized_pnl + realized)
    position.fees_paid = money(position.fees_paid + fee)
    if position.size <= 1e-9:
        position.size = 0.0
        position.cost_basis = 0.0
        position.status = PositionStatus.CLOSED.value
        position.closed_at = datetime.now(UTC)
    db.flush()

    pf.credit(db, order.participant_id, proceeds)
    pf.add_ledger(
        db,
        participant_id=order.participant_id,
        entry_type=LedgerType.TRADE_PROCEEDS,
        amount=proceeds,
        ref_type="order",
        ref_id=order.id,
        note=f"продажа {outcome} {walk.filled_size:.4f} @ {walk.avg_price:.4f}",
    )

    order.filled_size = walk.filled_size
    order.avg_fill_price = walk.avg_price
    order.notional = money(walk.filled_notional)
    order.fee = fee
    order.slippage_bps = walk.slippage_bps
    order.status = (
        OrderStatus.PARTIALLY_FILLED.value
        if walk.filled_size + 1e-9 < size_to_sell
        else OrderStatus.FILLED.value
    )
    db.flush()
    _write_fills(db, order, walk, fee)

    return ExecutionResult(
        order,
        walk.filled_size,
        walk.avg_price,
        money(walk.filled_notional),
        fee,
        OrderStatus(order.status),
        realized_pnl=realized,
    )


def cancel_order(db: Session, order: SimulatedOrder, reason: str = "отменено оператором") -> None:
    """Отмена ещё не исполненной заявки с возвратом резерва."""
    if order.status not in (OrderStatus.PENDING.value,):
        raise ValueError(f"нельзя отменить ордер в статусе {order.status}")
    pf.release(db, order.participant_id, order.requested_notional)
    order.status = OrderStatus.CANCELLED.value
    order.reject_reason = reason
    db.flush()
    audit.record(
        db,
        entity_type="simulated_order",
        entity_id=order.id,
        action="cancel",
        after={"reason": reason},
    )


def unrealized_pnl(db: Session, participant_id: int, marks: dict[tuple[int, str], float]) -> float:
    """Нереализованный PnL по открытым позициям при заданных mark-ценах."""
    total = 0.0
    for pos in pf.open_positions(db, participant_id):
        mark = marks.get((pos.market_id, pos.outcome), pos.avg_price)
        total += pos.size * mark - pos.cost_basis
    return money(total)


def latest_marks(db: Session) -> dict[tuple[int, str], float]:
    """Последние известные цены по каждому рынку — из самого свежего snapshot."""
    marks: dict[tuple[int, str], float] = {}
    market_ids = [m.id for m in db.scalars(select(Market))]
    for market_id in market_ids:
        snap = db.scalar(
            select(Snapshot)
            .where(Snapshot.market_id == market_id)
            .order_by(Snapshot.id.desc())
            .limit(1)
        )
        if snap is None:
            continue
        payload = snap.payload or {}
        marks[(market_id, "YES")] = float(payload.get("yes_price", 0.5))
        marks[(market_id, "NO")] = float(payload.get("no_price", 0.5))
    return marks
