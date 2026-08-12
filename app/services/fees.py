"""Комиссия Polymarket на спортивных рынках (`sports_fees_v2`).

Официальная формула:

    fee = C × rate × p × (1 − p)

где C — число контрактов, p — цена исполнения, rate берётся из `feeSchedule`
рынка (на матчах The International — 0.05 на всех типах рынков, от победителя
серии до тоталов и экзотики).

Три свойства, которые важны для расчётов:

* платит **только тейкер** (`takerOnly: true`) — мы всегда тейкер, потому что
  выкупаем стакан по рынку;
* комиссия берётся **в момент сделки**, а не при расчёте рынка;
* она максимальна у цены 0.5 и падает к краям — множитель p·(1−p)
  симметричен относительно середины.

Rebate по умолчанию 0: тир начинается с $2 000 weighted volume за 30 дней
(wV = размер × (1 − цена) × вес категории), а банк участника — $1 000, так что
до Bronze он не дотянется. Значение вынесено в конфиг на случай, если аккаунт
всё же наберёт оборот.

ВАЖНО: комиссия обязана участвовать в расчёте edge. Ставка по цене p выгодна
только при истинной вероятности q ≥ p + rate·p·(1−p) — иначе математическое
ожидание отрицательное даже при «правильном» прогнозе.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings

# Значения из feeSchedule рынков The International. Используются, когда
# провайдер не отдал собственную схему.
DEFAULT_SPORTS_FEE_RATE = 0.05
DEFAULT_FEE_EXPONENT = 1


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Схема комиссии конкретного рынка."""

    rate: float = DEFAULT_SPORTS_FEE_RATE
    exponent: int = DEFAULT_FEE_EXPONENT
    rebate_rate: float = 0.0
    taker_only: bool = True

    @classmethod
    def from_market_payload(cls, payload: object) -> FeeSchedule:
        """Разобрать `feeSchedule` из ответа Gamma; при мусоре — значения по умолчанию."""
        settings = get_settings()
        rebate = settings.polymarket_taker_rebate_rate
        if not isinstance(payload, dict):
            return cls(rate=settings.polymarket_taker_fee_rate, rebate_rate=rebate)

        def _num(key: str, default: float) -> float:
            try:
                value = float(payload.get(key))
            except (TypeError, ValueError):
                return default
            return value if value >= 0 else default

        return cls(
            rate=_num("rate", settings.polymarket_taker_fee_rate),
            exponent=int(_num("exponent", DEFAULT_FEE_EXPONENT)) or DEFAULT_FEE_EXPONENT,
            # rebateRate из рынка — доля, уходящая мейкерам; наш тир задаётся конфигом
            rebate_rate=rebate,
            taker_only=bool(payload.get("takerOnly", True)),
        )


def taker_fee(size: float, price: float, schedule: FeeSchedule | None = None) -> float:
    """Комиссия тейкера в USDC за сделку размером `size` контрактов по цене `price`."""
    schedule = schedule or FeeSchedule()
    if size <= 0 or not (0.0 < price < 1.0):
        return 0.0
    base = schedule.rate * (price * (1.0 - price)) ** schedule.exponent
    gross = size * base
    return round(gross * (1.0 - min(max(schedule.rebate_rate, 0.0), 1.0)), 6)


def fee_per_usdc(price: float, schedule: FeeSchedule | None = None) -> float:
    """Комиссия в долях от вложенной суммы: при 0.70 — около 1.5%.

    Удобнее для интуиции, чем комиссия на контракт: показывает, сколько
    процентов депозита съедает вход.
    """
    schedule = schedule or FeeSchedule()
    if not (0.0 < price < 1.0):
        return 0.0
    # fee / (size · price) = rate · (1 − price) при exponent = 1
    return round(taker_fee(1.0, price, schedule) / price, 6)


def breakeven_probability(price: float, schedule: FeeSchedule | None = None) -> float:
    """Минимальная вероятность исхода, при которой покупка по `price` не убыточна.

    Ожидание на контракт: q·1 − price − fee ≥ 0.
    """
    schedule = schedule or FeeSchedule()
    if not (0.0 < price < 1.0):
        return price
    return round(price + taker_fee(1.0, price, schedule), 6)


def net_edge(estimated_probability: float, price: float,
             schedule: FeeSchedule | None = None) -> float:
    """Edge после комиссии — то, ради чего вообще стоит входить в сделку.

    Положительное значение означает положительное матожидание на контракт.
    """
    return round(estimated_probability - breakeven_probability(price, schedule), 6)


def expected_value(size: float, estimated_probability: float, price: float,
                   schedule: FeeSchedule | None = None) -> float:
    """Матожидание сделки в USDC с учётом комиссии."""
    if size <= 0:
        return 0.0
    return round(size * net_edge(estimated_probability, price, schedule), 6)
