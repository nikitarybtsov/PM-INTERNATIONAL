"""Интерфейс источника рыночных данных.

ВАЖНО: провайдеры работают ТОЛЬКО на чтение. Ни один метод не отправляет и не
подписывает ордера; классы для этого не имеют ни ключей, ни соответствующих
методов.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class MarketNotAvailable(RuntimeError):
    """Рынок пропал, закрылся или изменился у источника."""


@dataclass(slots=True)
class MarketRef:
    """Нормализованное описание рынка (без котировок)."""

    source: str
    external_id: str
    title: str
    market_type: str = "MATCH_WINNER"
    slug: str | None = None
    event_title: str | None = None
    tournament: str | None = None
    team_a: str | None = None
    team_b: str | None = None
    yes_label: str = "YES"
    no_label: str = "NO"
    starts_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class MarketQuote:
    """Котировки и стакан на момент запроса."""

    yes_price: float
    no_price: float
    yes_bids: list[tuple[float, float]] = field(default_factory=list)
    yes_asks: list[tuple[float, float]] = field(default_factory=list)
    no_bids: list[tuple[float, float]] = field(default_factory=list)
    no_asks: list[tuple[float, float]] = field(default_factory=list)
    liquidity_usdc: float = 0.0
    volume_24h_usdc: float | None = None
    price_change_1h: float | None = None
    price_change_24h: float | None = None
    recent_prices: list[float] = field(default_factory=list)
    fetched_at: datetime | None = None
    raw: dict = field(default_factory=dict)


class MarketDataProvider(ABC):
    """Только чтение публичных данных."""

    name: str = "base"
    supports_live_orders: bool = False  # всегда False во всём проекте

    @abstractmethod
    def search_markets(self, query: str = "Dota", limit: int = 25) -> list[MarketRef]:
        """Найти подходящие рынки Dota 2 / The International."""

    @abstractmethod
    def get_market(self, external_id: str) -> MarketRef:
        """Получить рынок по идентификатору источника."""

    @abstractmethod
    def get_quote(self, market: MarketRef) -> MarketQuote:
        """Получить текущие цены, стакан и ликвидность."""

    def get_resolution(self, external_id: str) -> str | None:
        """Итог рынка, если биржа его уже рассчитала: "YES", "NO" или None.

        Работает одинаково для любого типа рынка — победителя серии, тотала,
        форы, экзотики: у всех бинарная структура, и Polymarket разрешает их
        сам. Оператору не нужно вводить результат руками.
        """
        return None

    def list_event_markets(self, market: MarketRef) -> list[MarketRef]:
        """Все рынки того же матча: карты, тоталы, форы, экзотика.

        Нужны, чтобы участник мог поставить не только на победителя серии.
        Стакан здесь не запрашивается — на матч приходится 20-30 рынков, и
        полный обход стаканов занял бы десятки запросов. Возвращаются метаданные
        и цены; стакан подтягивается уже для выбранного рынка.

        Провайдер, который так не умеет, возвращает пустой список — участники
        просто не увидят соседних рынков.
        """
        return []

    def close(self) -> None:  # pragma: no cover - по умолчанию нечего закрывать
        return None
