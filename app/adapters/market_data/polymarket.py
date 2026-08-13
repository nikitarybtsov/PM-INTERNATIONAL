"""Polymarket data adapter — ТОЛЬКО публичные read-only эндпоинты.

Используются:
  * Gamma API  (`/events`, `/markets`) — метаданные событий и рынков;
  * CLOB API   (`/book`)               — стакан по токену исхода.

Ключи и приватные ключи не требуются и намеренно не поддерживаются: адаптер
физически не умеет подписывать и отправлять ордера.

Поиск идёт по тегу Dota 2 в Gamma (`POLYMARKET_GAMMA_TAG_ID`, по умолчанию
102366), а не перебором всех активных рынков: на Polymarket одновременно живут
тысячи рынков, и выборка «первые N активных» не содержит Dota 2 вовсе.

У одного матча Polymarket публикует ~20-30 рынков (победитель серии, победитель
каждой карты, фора, тоталы, экзотика). Для эксперимента по умолчанию берётся
только основной рынок серии — см. `POLYMARKET_MAIN_MARKET_ONLY`.

Если рынок исчез, закрылся или сменил структуру — поднимается `MarketNotAvailable`,
раунд в таком случае не создаётся.
"""

from __future__ import annotations

import json
import logging
import re
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

# --- классификация типов рынка ---------------------------------------------
# Значения market_type попадают в статистику («прибыль по типам рынков»),
# поэтому набор фиксированный и стабильный.
MARKET_TYPE_MATCH = "MATCH_WINNER"
MARKET_TYPE_MAP = "MAP_WINNER"
MARKET_TYPE_HANDICAP = "HANDICAP"
MARKET_TYPE_TOTALS = "TOTALS"
MARKET_TYPE_SPECIAL = "SPECIAL"

_MAP_NUM_RE = re.compile(r"\b(game|map)\s*\d+\b", re.IGNORECASE)
_HANDICAP_RE = re.compile(r"handicap|\([+-]\d", re.IGNORECASE)
_TOTALS_RE = re.compile(r"\btotal\b|\bo/u\b|over/under|\bo\d|\bu\d", re.IGNORECASE)
# Признак основного рынка серии: формат Polymarket «… (BO3) - <турнир>».
_SERIES_RE = re.compile(r"\(bo\d\)", re.IGNORECASE)
_MAIN_TITLES = ("match winner", "series winner", "moneyline")


def _parse_dt(value: Any) -> datetime | None:
    """Gamma отдаёт время и как ISO с `T`, и как `2026-08-13 05:00:00+00`."""
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


def classify_market_type(question: str, event_title: str | None = None) -> str:
    """Определить тип рынка по его вопросу.

    Порядок проверок важен: «Game Handicap: TS (-1.5)…» содержит и слово game,
    и фору — это фора, а не победитель карты.
    """
    q = (question or "").strip()
    if not q:
        return MARKET_TYPE_SPECIAL
    # Точное совпадение с названием события — это основной рынок серии.
    if event_title and q.strip().lower() == event_title.strip().lower():
        return MARKET_TYPE_MATCH
    low = q.lower()
    if _HANDICAP_RE.search(low):
        return MARKET_TYPE_HANDICAP
    if _TOTALS_RE.search(low):
        return MARKET_TYPE_TOTALS
    # «Game 1 Winner» — победитель карты, но «Game 1: Ends in Daytime?» — экзотика.
    if _MAP_NUM_RE.search(low) and "winner" in low:
        return MARKET_TYPE_MAP
    if any(t in low for t in _MAIN_TITLES) or _SERIES_RE.search(low):
        return MARKET_TYPE_MATCH
    return MARKET_TYPE_SPECIAL


def _split_tournament(event_title: str | None, question: str) -> str | None:
    """«Dota 2: A vs B (BO3) - The International Group Stage» → турнир."""
    source = event_title or question or ""
    if " - " in source:
        tail = source.rsplit(" - ", 1)[-1].strip()
        if tail:
            return tail
    if "international" in source.lower():
        return "The International"
    return "Dota 2"


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
        tag_id: int | None = None,
        main_market_only: bool | None = None,
        only_upcoming: bool | None = None,
    ) -> None:
        s = get_settings()
        self.gamma_url = (gamma_url or s.polymarket_gamma_url).rstrip("/")
        self.clob_url = (clob_url or s.polymarket_clob_url).rstrip("/")
        self.timeout = timeout or s.polymarket_timeout_seconds
        self.tag_id = tag_id if tag_id is not None else s.polymarket_gamma_tag_id
        self.main_market_only = (
            main_market_only if main_market_only is not None else s.polymarket_main_market_only
        )
        self.only_upcoming = (
            only_upcoming if only_upcoming is not None else s.polymarket_only_upcoming
        )
        self.event_limit = s.polymarket_event_limit
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

    def _normalize(self, raw: dict, event: dict | None = None) -> MarketRef:
        outcomes = _as_list(raw.get("outcomes")) or ["Yes", "No"]
        title = raw.get("question") or raw.get("title") or raw.get("slug") or "unknown market"

        # `/markets/{id}` не отдаёт вложенные события, `/events` — отдаёт.
        if event is None:
            embedded = raw.get("events")
            if isinstance(embedded, list) and embedded and isinstance(embedded[0], dict):
                event = embedded[0]
        event_title = (event or {}).get("title")

        team_a = str(outcomes[0]) if len(outcomes) > 0 else None
        team_b = str(outcomes[1]) if len(outcomes) > 1 else None

        # Токены исходов: именно они торгуются на CLOB, рынок сам по себе нет.
        tokens = _as_list(raw.get("clobTokenIds"))
        yes_token = str(tokens[0]) if len(tokens) > 0 else None
        no_token = str(tokens[1]) if len(tokens) > 1 else None
        return MarketRef(
            source=self.name,
            external_id=str(raw.get("id") or raw.get("conditionId") or raw.get("slug")),
            slug=raw.get("slug") or (event or {}).get("slug"),
            title=title,
            market_type=classify_market_type(title, event_title),
            event_title=event_title,
            tournament=_split_tournament(event_title, title),
            team_a=team_a,
            team_b=team_b,
            yes_label=team_a or "YES",
            no_label=team_b or "NO",
            yes_token_id=yes_token,
            no_token_id=no_token,
            starts_at=_parse_dt(
                raw.get("gameStartTime")
                or raw.get("startDate")
                or (event or {}).get("startDate")
            ),
            raw=raw,
        )

    # ---- отбор -------------------------------------------------------------
    @staticmethod
    def _is_tradeable(raw: dict) -> bool:
        if raw.get("closed") is True or raw.get("archived") is True:
            return False
        if raw.get("active") is False:
            return False
        return True

    def _matches_query(self, query: str, ref: MarketRef) -> bool:
        """Пустой запрос и «dota» пропускают всё; иначе — подстрока.

        Позволяет оператору сузить поиск до конкретного турнира, например
        `POLYMARKET_SEARCH_QUERY="The International"`.
        """
        q = (query or "").strip().lower()
        if not q or q in ("dota", "dota2", "dota 2"):
            return True
        haystack = " ".join(
            str(x) for x in (ref.title, ref.event_title, ref.tournament, ref.slug) if x
        ).lower()
        return q in haystack

    # ---- публичный интерфейс ----------------------------------------------
    def search_markets(self, query: str = "Dota", limit: int = 25) -> list[MarketRef]:
        """Найти рынки Dota 2 / The International по тегу Gamma.

        По умолчанию возвращает только основной рынок серии каждого матча и
        только матчи, которые ещё не начались.
        """
        payload = self._get(
            f"{self.gamma_url}/events",
            params={
                "tag_id": self.tag_id,
                "closed": "false",
                "limit": self.event_limit,
            },
        )
        events = payload if isinstance(payload, list) else payload.get("data", [])
        self._store_raw(
            "search",
            "/events",
            {"tag_id": self.tag_id, "events": len(events), "query": query},
        )

        now = datetime.now(UTC)
        refs: list[MarketRef] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            markets = event.get("markets")
            if not isinstance(markets, list):
                continue
            for raw in markets:
                if not isinstance(raw, dict) or not self._is_tradeable(raw):
                    continue
                try:
                    ref = self._normalize(raw, event=event)
                except Exception:  # pragma: no cover - пропускаем битые записи
                    logger.warning("пропущен рынок с неожиданной структурой")
                    continue
                if self.main_market_only and ref.market_type != MARKET_TYPE_MATCH:
                    continue
                if self.only_upcoming and (ref.starts_at is None or ref.starts_at <= now):
                    continue
                if not self._matches_query(query, ref):
                    continue
                refs.append(ref)

        refs.sort(key=lambda r: (r.starts_at or datetime.max.replace(tzinfo=UTC)))
        if not refs:
            logger.info(
                "по тегу %s не найдено предстоящих рынков (query=%r)", self.tag_id, query
            )
        return refs[:limit]

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

    def get_resolution(self, external_id: str) -> str | None:
        """Итог рынка по данным биржи.

        Закрытый и разрешённый рынок отдаёт outcomePrices ["1","0"] или
        ["0","1"] — это и есть результат. Пока рынок не закрыт или UMA ещё не
        подтвердила исход, возвращается None: гасить позиции рано.
        """
        try:
            payload = self._get(f"{self.gamma_url}/markets/{external_id}")
        except MarketNotAvailable:
            return None
        raw = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(raw, dict) or raw.get("closed") is not True:
            return None

        status = str(raw.get("umaResolutionStatus") or "").lower()
        if status and status != "resolved":
            # рынок закрыт, но исход ещё оспаривается — ждём
            logger.info("рынок %s закрыт, но не разрешён (%s)", external_id, status)
            return None

        prices = [_to_float(p) for p in _as_list(raw.get("outcomePrices"))]
        if len(prices) < 2:
            return None
        if prices[0] >= 0.99 and prices[1] <= 0.01:
            return "YES"
        if prices[1] >= 0.99 and prices[0] <= 0.01:
            return "NO"
        # промежуточные цены означают, что рынок ещё не рассчитан окончательно
        return None

    def list_event_markets(self, market: MarketRef) -> list[MarketRef]:
        """Все рынки события одним запросом к Gamma.

        Событие определяется по slug: у Polymarket рынки матча лежат внутри
        одного события, и `/events?slug=…` отдаёт их разом вместе с ценами.
        """
        slug = (market.raw or {}).get("slug") or market.slug
        event_slug = (market.raw or {}).get("events", [{}])
        if isinstance(event_slug, list) and event_slug:
            slug = (event_slug[0] or {}).get("slug") or slug
        if not slug:
            return []

        try:
            payload = self._get(f"{self.gamma_url}/events", params={"slug": slug})
        except MarketNotAvailable:
            logger.warning("не удалось получить рынки события %s", slug)
            return []

        events = payload if isinstance(payload, list) else [payload]
        refs: list[MarketRef] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            for raw in event.get("markets") or []:
                if not isinstance(raw, dict) or not self._is_tradeable(raw):
                    continue
                if str(raw.get("id")) == str(market.external_id):
                    continue  # сам рынок раунда в список соседей не попадает
                try:
                    refs.append(self._normalize(raw, event=event))
                except Exception:  # pragma: no cover - битые записи пропускаем
                    continue
        self._store_raw(market.external_id, "/events(siblings)", {"count": len(refs)})
        return refs

    def _fetch_price_history(self, token_id: str) -> list[float]:
        """История цены исхода YES — поле `recent_prices` снимка.

        Без неё участники не видят, двигался ли рынок, и не могут отличить
        стабильную цену от свежего сдвига на новостях.
        """
        settings = get_settings()
        payload = self._get(
            f"{self.clob_url}/prices-history",
            params={
                "market": token_id,
                "interval": settings.polymarket_history_interval,
                "fidelity": settings.polymarket_history_fidelity,
            },
        )
        points = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(points, list):
            return []
        prices = [
            round(_to_float(p.get("p")), 4)
            for p in points
            if isinstance(p, dict) and p.get("p") is not None
        ]
        return prices[-settings.polymarket_history_points :]

    def get_quote(self, market: MarketRef) -> MarketQuote:
        raw = market.raw or {}
        token_ids = _as_list(raw.get("clobTokenIds"))
        prices = [_to_float(p) for p in _as_list(raw.get("outcomePrices"))]

        yes_price = prices[0] if prices else _to_float(raw.get("lastTradePrice"), 0.5)
        no_price = prices[1] if len(prices) > 1 else round(1.0 - yes_price, 4)

        yes_bids: list[tuple[float, float]] = []
        yes_asks: list[tuple[float, float]] = []
        no_bids: list[tuple[float, float]] = []
        no_asks: list[tuple[float, float]] = []
        recent_prices: list[float] = []
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
        if token_ids:
            # История — не критичный для сделки блок: её отсутствие не должно
            # мешать зафиксировать snapshot.
            try:
                recent_prices = self._fetch_price_history(str(token_ids[0]))
                self._store_raw(
                    market.external_id, "/prices-history", {"points": len(recent_prices)}
                )
            except MarketNotAvailable:
                logger.warning("история цен недоступна для %s", market.external_id)

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
            recent_prices=recent_prices,
            fetched_at=datetime.now(UTC),
            raw={"outcomePrices": prices, "clobTokenIds": token_ids},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
