"""Реальный портфель участника на Polymarket.

В боевом режиме источник истины — биржа, а не наша симуляция. Если ордер не
дошёл, бумажный баланс покажет ставку, которой на самом деле нет: ровно так
scoreboard показывал у Codex «999.44 $ и одна ставка», когда на бирже не было
ничего.

Данные берутся из публичного Data API по адресу кошелька — ключи не нужны:
  * `/value`     — суммарная стоимость открытых позиций;
  * `/positions` — сами позиции.

Свободный USDC оттуда не виден, он читается через торговый адаптер.

Сеть может не ответить, поэтому любая ошибка возвращает `None`, а не роняет
страницу: панель в таком случае честно скажет, что данных с биржи нет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"


@dataclass(slots=True)
class LivePortfolio:
    """Что реально лежит на кошельке участника."""

    participant: str
    address: str
    cash_usdc: float | None = None
    positions_value_usdc: float | None = None
    positions: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def equity_usdc(self) -> float | None:
        """Итог по бирже. None, если данных не хватает.

        Свободный USDC виден только через торговый ключ: Polymarket держит его
        во внутреннем учёте, на самом адресе баланс нулевой, а публичного
        эндпоинта нет. У Титана ключа нет, поэтому итог для него не считается —
        показывать сумму без наличных значило бы врать.
        """
        if self.cash_usdc is None or self.positions_value_usdc is None:
            return None
        return round(self.cash_usdc + self.positions_value_usdc, 2)

    @property
    def open_positions(self) -> list[dict]:
        """Позиции, которые ещё чего-то стоят.

        В выдаче лежат и давно рассчитанные рынки с нулевой стоимостью — на
        кошельках, где раньше работали другие боты, их десятки.
        """
        live = []
        for position in self.positions:
            try:
                if float(position.get("value") or 0) > 0.01:
                    live.append(position)
            except (TypeError, ValueError):
                continue
        return live


def _address_for(participant: str) -> str | None:
    """Адрес, по которому биржа знает участника."""
    settings = get_settings()
    if participant == "titan":
        return settings.titan_poly_address
    wallet = settings.wallet_for(participant)
    return wallet.funder if wallet else None


def fetch(participant: str, *, timeout: float = 12.0) -> LivePortfolio | None:
    """Портфель участника с биржи. None — если адрес не настроен."""
    address = _address_for(participant)
    if not address:
        return None

    result = LivePortfolio(participant=participant, address=address)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            value = client.get(f"{DATA_API}/value", params={"user": address})
            if value.status_code == 200:
                rows = value.json()
                if isinstance(rows, list) and rows:
                    result.positions_value_usdc = round(float(rows[0].get("value") or 0), 2)

            positions = client.get(
                f"{DATA_API}/positions", params={"user": address, "limit": 50}
            )
            if positions.status_code == 200:
                rows = positions.json()
                if isinstance(rows, list):
                    result.positions = [
                        {
                            "title": str(p.get("title") or "")[:80],
                            "outcome": p.get("outcome"),
                            "size": p.get("size"),
                            "avg_price": p.get("avgPrice"),
                            "value": p.get("currentValue"),
                        }
                        for p in rows
                    ]
    except Exception as exc:  # noqa: BLE001 — биржа не должна ронять панель
        logger.warning("портфель %s не получен: %s", participant, exc)
        result.error = str(exc)[:200]
        return result

    # Свободный USDC виден только торговому адаптеру (нужна подпись).
    if participant != "titan":
        try:
            from app.adapters.execution import PolymarketLiveAdapter

            result.cash_usdc = PolymarketLiveAdapter().balance_usdc(participant)
        except Exception as exc:  # noqa: BLE001
            logger.warning("баланс %s не прочитан: %s", participant, exc)
    return result


def fetch_all() -> dict[str, LivePortfolio]:
    """Портфели всех участников, у кого настроен адрес."""
    out: dict[str, LivePortfolio] = {}
    for key in ("codex", "claude", "titan"):
        portfolio = fetch(key)
        if portfolio is not None:
            out[key] = portfolio
    return out
