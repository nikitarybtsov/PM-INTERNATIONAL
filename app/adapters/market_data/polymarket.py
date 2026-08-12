"""Polymarket data adapter — ТОЛЬКО публичные read-only эндпоинты.

Используются:
  * Gamma API  (`/markets`, `/events`) — метаданные рынков;
  * CLOB API   (`/book`)               — стакан по токену исхода.

Ключи и приватные ключи не требуются и намеренно не поддерживаются: адаптер
физически не умеет подписывать и отправлять ордера.

Если рынок исчез, закрылся или сменил структуру — поднимается `MarketNotAvailable`,
раунд в таком случае не создаётся.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.adapters.market_data.base import (
    MarketDataProvider,
    MarketNotAvailable,
    MarketQuote,
    MarketRef,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

_DOTA_HINTS = ("dota", "the international", "ti20", "ti 20")


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _as_list(value: Any) -> list:
    """Gamma отдаёт часть полей строкой с JSON внутри."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PolymarketDataProvider(MarketDataProvider):
    name = "polymarket"
    supports_live_orders = False

    def __init__(
        self,
        gamma_url: str | None = None,
        clob_url: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
        raw_sink: Any = None,
    ) -> None:
        s = get_settings()
        self.gamma_url = (gamma_url or s.polymarket_gamma_url).rstrip("/")
        self.clob_url = (clob_url or s.polymarket_clob_url).rstrip("/")
        self.timeout = timeout or s.polymarket_timeout_seconds
        self._client = client or httpx.Client(timeout=self.timeout, follow_redirects=True)
        self._owns_client = client is None
        # callable(source, external_id, endpoint, payload) — сохранение сырых ответов
        self.raw_sink = raw_sink

    # ---- низкий уровень ----------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise MarketNotAvailable(f"Polymarket недоступен ({url}): {exc}") from exc
        except json.JSONDecodeError as exc:
            raise MarketNotAvailable(f"Polymarket вернул не-JSON ({url})") from exc

    def _store_raw(self, external_id: str, endpoint: str, payload: Any) -> None:
        if self.raw_sink is None:
            return
        try:
            self.raw_sink(self.name, external_id, endpoint, payload)
        except Exception:  # pragma: no cover - аудит не должен ломать поток
            logger.exception("не удалось сохранить сырой ответ Polymarket")

    # ---- нормализация ------------------------------------------------------
    @staticmethod
    def _looks_like_dota(text: str) -> bool:
        low = text.lower()
        return any(hint in low for hint in _DOTA_HINTS)

    def _normalize(self, raw: dict) -> MarketRef:
        outcomes = _as_list(raw.get("outcomes")) or ["Yes", "No"]
        title = raw.get("question") or raw.get("title") or raw.get("slug") or "unknown market"
        event = raw.get("events") or []
        event_title = None
        if isinstance(event, list) and event:
            event_title = (event[0] or {}).get("title")
        team_a = str(outcomes[0]) if len(outcomes) > 0 else None
        team_b = str(outcomes[1]) if len(outcomes) > 1 else None
        return MarketRef(
            source=self.name,
            external_id=str(raw.get("id") or raw.get("conditionId") or raw.get("slug")),
            slug=raw.get("slug"),
            title=title,
            market_type=raw.get("marketType") or "MATCH_WINNER",
            event_title=event_title,
            tournament="The International" if "international" in title.lower() else "Dota 2",
            team_a=team_a,
            team_b=team_b,
            yes_label=team_a or "YES",
            no_label=team_b or "NO",
            starts_at=_parse_dt(raw.get("gameStartTime") or raw.get("startDate")),
            raw=raw,
        )

    # ---- публичный интерфейс ----------------------------------------------
    def search_markets(self, query: str = "Dota", limit: int = 25) -> list[MarketRef]:
        payload = self._get(
            f"{self.gamma_url}/markets",
            params={"active": "true", "closed": "false", "limit": max(limit * 4, 50)},
        )
        items = payload if isinstance(payload, list) else payload.get("data", [])
        self._store_raw("search", "/markets", {"count": len(items), "query": query})

        refs: list[MarketRef] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            haystack = " ".join(
                str(raw.get(field, "")) for field in ("question", "title", "slug", "description")
            )
            if not (self._looks_like_dota(haystack) or query.lower() in haystack.lower()):
                continue
            try:
                refs.append(self._normalize(raw))
            except Exception:  # pragma: no cover - пропускаем битые записи
                logger.warning("пропущен рынок с неожиданной структурой")
            if len(refs) >= limit:
                break
        return refs

    def get_market(self, external_id: str) -> MarketRef:
        payload = self._get(f"{self.gamma_url}/markets/{external_id}")
        if not payload or (isinstance(payload, dict) and payload.get("error")):
            raise MarketNotAvailable(f"рынок {external_id} отсутствует у Polymarket")
        raw = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(raw, dict):
            raise MarketNotAvailable(f"неожиданный формат ответа для {external_id}")
        if raw.get("closed") is True:
            logger.info("рынок %s закрыт у источника", external_id)
        self._store_raw(external_id, f"/markets/{external_id}", raw)
        return self._normalize(raw)

    def _fetch_book(self, token_id: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        payload = self._get(f"{self.clob_url}/book", params={"token_id": token_id})
        if not isinstance(payload, dict):
            return [], []
        bids = [
            (round(_to_float(lv.get("price")), 4), round(_to_float(lv.get("size")), 6))
            for lv in payload.get("bids", [])
            if isinstance(lv, dict)
        ]
        asks = [
            (round(_to_float(lv.get("price")), 4), round(_to_float(lv.get("size")), 6))
            for lv in payload.get("asks", [])
            if isinstance(lv, dict)
        ]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return bids[:10], asks[:10]

    def get_quote(self, market: MarketRef) -> MarketQuote:
        raw = market.raw or {}
        token_ids = _as_list(raw.get("clobTokenIds"))
        prices = [_to_float(p) for p in _as_list(raw.get("outcomePrices"))]

        yes_price = prices[0] if prices else _to_float(raw.get("lastTradePrice"), 0.5)
        no_price = prices[1] if len(prices) > 1 else round(1.0 - yes_price, 4)

        yes_bids = yes_asks = no_bids = no_asks = []
        if len(token_ids) >= 2:
            try:
                yes_bids, yes_asks = self._fetch_book(str(token_ids[0]))
                no_bids, no_asks = self._fetch_book(str(token_ids[1]))
                self._store_raw(
                    market.external_id,
                    "/book",
                    {"yes_levels": len(yes_asks), "no_levels": len(no_asks)},
                )
            except MarketNotAvailable:
                logger.warning("стакан недоступен для %s, используем только цены", market.external_id)

        if yes_asks:
            yes_price = yes_asks[0][0]
        if no_asks:
            no_price = no_asks[0][0]

        liquidity = _to_float(raw.get("liquidityNum") or raw.get("liquidity"))
        if not liquidity:
            liquidity = round(
                sum(p * s for p, s in yes_asks) + sum(p * s for p, s in no_asks), 2
            )

        return MarketQuote(
            yes_price=round(min(max(yes_price, 0.0), 1.0), 4),
            no_price=round(min(max(no_price, 0.0), 1.0), 4),
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
            liquidity_usdc=liquidity,
            volume_24h_usdc=_to_float(raw.get("volume24hr") or raw.get("volumeNum")) or None,
            price_change_1h=_to_float(raw.get("oneHourPriceChange")) or None,
            price_change_24h=_to_float(raw.get("oneDayPriceChange")) or None,
            recent_prices=[],
            fetched_at=datetime.now(UTC),
            raw={"outcomePrices": prices, "clobTokenIds": token_ids},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
