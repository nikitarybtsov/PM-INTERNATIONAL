"""Работа со стаканом: детерминированный обход уровней.

Используется и risk engine (для оценки проскальзывания), и paper engine
(для собственно исполнения) — чтобы оценка и факт совпадали.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import price as round_price
from app.constants import size as round_size
from app.schemas.snapshot import BookLevel


@dataclass(slots=True)
class LevelFill:
    level_index: int
    price: float
    size: float
    notional: float


@dataclass(slots=True)
class WalkResult:
    fills: list[LevelFill]
    filled_size: float
    filled_notional: float
    avg_price: float
    reference_price: float | None
    remaining_notional: float
    remaining_size: float
    limited_by_price: bool
    limited_by_depth: bool

    @property
    def is_empty(self) -> bool:
        return self.filled_size <= 0

    @property
    def slippage_bps(self) -> float:
        """Отклонение средней цены от лучшей котировки, в базисных пунктах."""
        if self.reference_price is None or self.reference_price <= 0 or self.is_empty:
            return 0.0
        return round(abs(self.avg_price - self.reference_price) / self.reference_price * 10_000, 2)


def walk_buy(levels: list[BookLevel], notional_budget: float, max_price: float | None) -> WalkResult:
    """Купить на заданную сумму USDC, идя по asks снизу вверх."""
    fills: list[LevelFill] = []
    budget = round(max(notional_budget, 0.0), 6)
    spent = 0.0
    bought = 0.0
    limited_by_price = False
    reference = levels[0].price if levels else None

    for idx, level in enumerate(levels):
        if budget <= 1e-9:
            break
        if max_price is not None and level.price > max_price + 1e-9:
            limited_by_price = True
            break
        if level.price <= 0:
            continue
        level_notional = level.price * level.size
        take_notional = min(budget, level_notional)
        take_size = take_notional / level.price
        if take_size <= 0:
            continue
        take_size = round_size(take_size)
        take_notional = round(take_size * level.price, 6)
        fills.append(LevelFill(idx, round_price(level.price), take_size, take_notional))
        spent += take_notional
        bought += take_size
        budget = round(budget - take_notional, 6)

    avg = round_price(spent / bought) if bought > 0 else 0.0
    return WalkResult(
        fills=fills,
        filled_size=round_size(bought),
        filled_notional=round(spent, 6),
        avg_price=avg,
        reference_price=reference,
        remaining_notional=round(max(budget, 0.0), 6),
        remaining_size=0.0,
        limited_by_price=limited_by_price,
        limited_by_depth=budget > 1e-6 and not limited_by_price,
    )


def walk_sell(levels: list[BookLevel], size_to_sell: float, min_price: float | None) -> WalkResult:
    """Продать заданное количество контрактов, идя по bids сверху вниз."""
    fills: list[LevelFill] = []
    remaining = round_size(max(size_to_sell, 0.0))
    proceeds = 0.0
    sold = 0.0
    limited_by_price = False
    reference = levels[0].price if levels else None

    for idx, level in enumerate(levels):
        if remaining <= 1e-9:
            break
        if min_price is not None and level.price < min_price - 1e-9:
            limited_by_price = True
            break
        take_size = round_size(min(remaining, level.size))
        if take_size <= 0:
            continue
        take_notional = round(take_size * level.price, 6)
        fills.append(LevelFill(idx, round_price(level.price), take_size, take_notional))
        proceeds += take_notional
        sold += take_size
        remaining = round_size(remaining - take_size)

    avg = round_price(proceeds / sold) if sold > 0 else 0.0
    return WalkResult(
        fills=fills,
        filled_size=round_size(sold),
        filled_notional=round(proceeds, 6),
        avg_price=avg,
        reference_price=reference,
        remaining_notional=0.0,
        remaining_size=remaining,
        limited_by_price=limited_by_price,
        limited_by_depth=remaining > 1e-6 and not limited_by_price,
    )
