"""Mock-провайдер: демо-рынки The International без доступа к сети.

Детерминирован: цены зависят от external_id и номера обращения, а не от
системного времени, поэтому демо воспроизводимо.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from app.adapters.market_data.base import (
    MarketDataProvider,
    MarketNotAvailable,
    MarketQuote,
    MarketRef,
)

_BASE_TIME = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

_DEMO_MARKETS: list[dict] = [
    {
        "external_id": "ti-demo-001",
        "title": "Team Spirit vs Falcons — победитель серии (TI Playoffs)",
        "team_a": "Team Spirit",
        "team_b": "Falcons",
        "market_type": "MATCH_WINNER",
        "yes_price": 0.58,
        "starts_in_hours": 3,
        "liquidity": 24000.0,
    },
    {
        "external_id": "ti-demo-002",
        "title": "Xtreme Gaming vs BetBoom — победитель серии (TI Playoffs)",
        "team_a": "Xtreme Gaming",
        "team_b": "BetBoom Team",
        "market_type": "MATCH_WINNER",
        "yes_price": 0.44,
        "starts_in_hours": 6,
        "liquidity": 15500.0,
    },
    {
        "external_id": "ti-demo-003",
        "title": "Team Liquid vs Tundra — победитель карты 2",
        "team_a": "Team Liquid",
        "team_b": "Tundra Esports",
        "market_type": "MAP_WINNER",
        "yes_price": 0.51,
        "starts_in_hours": 1,
        "liquidity": 9200.0,
    },
    {
        "external_id": "ti-demo-004",
        "title": "Gaimin Gladiators выйдут в верхнюю сетку TI",
        "team_a": "Gaimin Gladiators",
        "team_b": "Поле",
        "market_type": "QUALIFICATION",
        "yes_price": 0.31,
        "starts_in_hours": 30,
        "liquidity": 6100.0,
    },
    {
        "external_id": "ti-demo-005",
        "title": "PSG.Quest vs Nigma Galaxy — победитель серии (TI Group Stage)",
        "team_a": "PSG.Quest",
        "team_b": "Nigma Galaxy",
        "market_type": "MATCH_WINNER",
        "yes_price": 0.67,
        "starts_in_hours": 12,
        "liquidity": 4300.0,
    },
]


def _seeded_offset(key: str, spread: float = 0.02) -> float:
    digest = hashlib.sha256(key.encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0..1
    return round((unit - 0.5) * 2 * spread, 4)


def _build_book(mid: float, liquidity: float, key: str) -> tuple[list, list]:
    """Симметричный стакан из 4 уровней вокруг mid."""
    half_spread = 0.01
    bids, asks = [], []
    for i in range(4):
        step = round(half_spread + i * 0.01, 4)
        bid_p = round(max(0.01, mid - step), 4)
        ask_p = round(min(0.99, mid + step), 4)
        depth_usdc = round(liquidity * (0.30 - i * 0.06) * (1 + _seeded_offset(f"{key}:{i}", 0.15)), 2)
        depth_usdc = max(depth_usdc, 25.0)
        bids.append((bid_p, round(depth_usdc / max(bid_p, 0.01), 6)))
        asks.append((ask_p, round(depth_usdc / max(ask_p, 0.01), 6)))
    return bids, asks


class MockMarketDataProvider(MarketDataProvider):
    name = "mock"

    def __init__(self, markets: list[dict] | None = None) -> None:
        self._markets = markets if markets is not None else _DEMO_MARKETS
        self._calls: dict[str, int] = {}

    # ---- поиск / получение ------------------------------------------------
    def _to_ref(self, cfg: dict) -> MarketRef:
        return MarketRef(
            source=self.name,
            external_id=cfg["external_id"],
            slug=cfg["external_id"],
            title=cfg["title"],
            market_type=cfg.get("market_type", "MATCH_WINNER"),
            event_title="The International (демо-данные)",
            tournament="The International",
            team_a=cfg.get("team_a"),
            team_b=cfg.get("team_b"),
            yes_label=cfg.get("team_a") or "YES",
            no_label=cfg.get("team_b") or "NO",
            starts_at=_BASE_TIME + timedelta(hours=cfg.get("starts_in_hours", 4)),
            raw={"provider": "mock", **cfg},
        )

    def search_markets(self, query: str = "Dota", limit: int = 25) -> list[MarketRef]:
        q = (query or "").strip().lower()
        refs = [self._to_ref(cfg) for cfg in self._markets]
        if q and q not in ("dota", "the international", "ti"):
            refs = [
                r
                for r in refs
                if q in r.title.lower()
                or q in (r.team_a or "").lower()
                or q in (r.team_b or "").lower()
            ]
        return refs[:limit]

    def get_market(self, external_id: str) -> MarketRef:
        for cfg in self._markets:
            if cfg["external_id"] == external_id:
                return self._to_ref(cfg)
        raise MarketNotAvailable(f"mock-рынок {external_id} не найден")

    def get_quote(self, market: MarketRef) -> MarketQuote:
        cfg = next((c for c in self._markets if c["external_id"] == market.external_id), None)
        if cfg is None:
            raise MarketNotAvailable(f"mock-рынок {market.external_id} исчез у источника")

        n = self._calls.get(market.external_id, 0)
        self._calls[market.external_id] = n + 1

        base = float(cfg["yes_price"])
        drift = _seeded_offset(f"{market.external_id}:{n}", 0.03)
        yes = round(min(0.97, max(0.03, base + drift)), 4)
        no = round(1.0 - yes, 4)
        liq = float(cfg.get("liquidity", 5000.0))

        yes_bids, yes_asks = _build_book(yes, liq, f"{market.external_id}:yes:{n}")
        no_bids, no_asks = _build_book(no, liq, f"{market.external_id}:no:{n}")
        history = [
            round(min(0.97, max(0.03, base + _seeded_offset(f"{market.external_id}:h{i}", 0.04))), 4)
            for i in range(6)
        ] + [yes]

        return MarketQuote(
            yes_price=yes,
            no_price=no,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
            liquidity_usdc=liq,
            volume_24h_usdc=round(liq * 3.2, 2),
            price_change_1h=round(yes - history[-2], 4),
            price_change_24h=round(yes - history[0], 4),
            recent_prices=history,
            fetched_at=datetime.now(UTC),
            raw={"provider": "mock", "call_index": n, "external_id": market.external_id},
        )
