"""Схема immutable-снимка рынка.

Snapshot формируется ОДИН раз перед раундом и отдаётся всем трём участникам
в абсолютно идентичном виде. Хеш payload'а фиксируется в БД, что позволяет
доказать: Codex, Claude и Titan видели одни и те же данные.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constants import Phase


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0, description="Размер уровня в контрактах")

    @property
    def notional(self) -> float:
        return round(self.price * self.size, 6)


class SnapshotBook(BaseModel):
    """Стакан по исходу. bids — где можно продать, asks — где можно купить."""

    model_config = ConfigDict(frozen=True)

    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def ask_depth_usdc(self) -> float:
        return round(sum(level.notional for level in self.asks), 6)

    @property
    def bid_depth_usdc(self) -> float:
        return round(sum(level.notional for level in self.bids), 6)


class SnapshotMarketInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_id: int
    external_id: str
    source: str
    title: str
    event_title: str | None = None
    tournament: str | None = None
    market_type: str = "MATCH_WINNER"
    team_a: str | None = None
    team_b: str | None = None
    yes_label: str = "YES"
    no_label: str = "NO"
    starts_at: datetime | None = None


class SiblingMarket(BaseModel):
    """Другой рынок того же матча: тотал карт, фора, победитель карты, экзотика.

    Участник волен ставить на любой из них — недооценённым может оказаться не
    победитель серии, а, скажем, тотал. Стакан здесь не приводится: он занял бы
    весь промпт, а для отбора кандидата хватает цены, спреда и ликвидности.
    Полный стакан подтягивается уже при исполнении.
    """

    model_config = ConfigDict(frozen=True)

    market_id: int
    external_id: str
    question: str
    market_type: str = "SPECIAL"
    yes_label: str = "YES"
    no_label: str = "NO"
    yes_price: float = Field(ge=0.0, le=1.0)
    no_price: float = Field(ge=0.0, le=1.0)
    yes_best_ask: float | None = None
    no_best_ask: float | None = None
    liquidity_usdc: float = 0.0

    #: Стаканы. Заполняются только для рынков, на которых разрешено исполнение:
    #: без заявок невозможно посчитать среднюю цену и проскальзывание, а значит
    #: и купить. Пустые книги означают «рынок виден, но ставить нельзя».
    yes_book: SnapshotBook = Field(default_factory=SnapshotBook)
    no_book: SnapshotBook = Field(default_factory=SnapshotBook)

    @property
    def is_executable(self) -> bool:
        """Есть ли стакан, по которому можно исполнить заявку."""
        return bool(self.yes_book.asks or self.no_book.asks)

    @property
    def spread(self) -> float | None:
        if self.yes_best_ask is None:
            return None
        return round(abs(self.yes_best_ask - self.yes_price) * 2, 4)


class MarketSnapshot(BaseModel):
    """Полный снимок, который видят участники."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: int | None = None
    phase: Phase
    map_number: int | None = None
    market: SnapshotMarketInfo

    #: Остальные рынки того же матча. Участник может выбрать любой из них,
    #: указав market_id в решении.
    sibling_markets: list[SiblingMarket] = Field(default_factory=list)

    yes_price: float = Field(ge=0.0, le=1.0, description="Средняя цена YES (mid)")
    no_price: float = Field(ge=0.0, le=1.0, description="Средняя цена NO (mid)")
    yes_book: SnapshotBook
    no_book: SnapshotBook

    liquidity_usdc: float = Field(ge=0.0, default=0.0)
    volume_24h_usdc: float | None = None
    price_change_1h: float | None = Field(
        default=None, description="Изменение цены YES за последний час, в абсолютных пунктах"
    )
    price_change_24h: float | None = None
    recent_prices: list[float] = Field(default_factory=list)

    operator_context: str | None = Field(
        default=None,
        description="Известная оператору информация: составы, замены, новости турнира",
    )

    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int = 900

    # ---- производные величины ------------------------------------------------
    @field_validator("recent_prices")
    @classmethod
    def _limit_history(cls, v: list[float]) -> list[float]:
        return v[-50:]

    @property
    def yes_spread(self) -> float | None:
        bid, ask = self.yes_book.best_bid, self.yes_book.best_ask
        return round(ask - bid, 6) if bid is not None and ask is not None else None

    def best_ask_for(self, outcome: str) -> float | None:
        book = self.yes_book if outcome == "YES" else self.no_book
        return book.best_ask

    def best_bid_for(self, outcome: str) -> float | None:
        book = self.yes_book if outcome == "YES" else self.no_book
        return book.best_bid

    def book_for(self, outcome: str, market_id: int | None = None) -> SnapshotBook:
        """Стакан исхода. По умолчанию — основной рынок раунда.

        Для выбранного участником соседнего рынка возвращается ЕГО стакан:
        исполнять заявку по чужим заявкам нельзя — купится не то и не по той цене.
        """
        if market_id is not None and market_id != self.market.market_id:
            sibling = self.sibling(market_id)
            if sibling is not None:
                return sibling.yes_book if outcome == "YES" else sibling.no_book
            return SnapshotBook()
        return self.yes_book if outcome == "YES" else self.no_book

    def is_executable_market(self, market_id: int | None) -> bool:
        """Можно ли исполнить заявку на этом рынке — есть ли у нас его стакан."""
        if market_id is None or market_id == self.market.market_id:
            return True
        sibling = self.sibling(market_id)
        return sibling is not None and sibling.is_executable

    def market_probability(self, outcome: str, market_id: int | None = None) -> float:
        """Цена исхода. По умолчанию — основной рынок раунда.

        Если участник выбрал другой рынок матча, цена берётся из него: сравнивать
        его оценку с ценой чужого рынка бессмысленно.
        """
        if market_id is not None and market_id != self.market.market_id:
            sibling = self.sibling(market_id)
            if sibling is not None:
                return sibling.yes_price if outcome == "YES" else sibling.no_price
        return self.yes_price if outcome == "YES" else self.no_price

    def sibling(self, market_id: int) -> SiblingMarket | None:
        for candidate in self.sibling_markets:
            if candidate.market_id == market_id:
                return candidate
        return None

    def allows_market(self, market_id: int | None) -> bool:
        """Можно ли ставить на этот рынок в рамках снимка."""
        if market_id is None or market_id == self.market.market_id:
            return True
        return self.sibling(market_id) is not None

    def tradeable_market_ids(self) -> list[int]:
        return [self.market.market_id, *(s.market_id for s in self.sibling_markets)]

    def payload(self) -> dict:
        """JSON-совместимое представление для хранения в БД."""
        return json.loads(self.model_dump_json())

    def content_hash(self) -> str:
        data = self.payload()
        data.pop("snapshot_id", None)
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def is_stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        captured = self.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        return (now - captured).total_seconds() > self.ttl_seconds

    def to_prompt_dict(self) -> dict:
        """Компактное представление для промптов участников (одинаковое для всех)."""
        m = self.market
        return {
            "snapshot_id": self.snapshot_id,
            "phase": self.phase.value,
            "map_number": self.map_number,
            "market_id": m.market_id,
            "market_title": m.title,
            "market_type": m.market_type,
            "tournament": m.tournament,
            "teams": {"a": m.team_a, "b": m.team_b},
            "outcome_labels": {"YES": m.yes_label, "NO": m.no_label},
            "match_start_time": m.starts_at.isoformat() if m.starts_at else None,
            "prices": {"yes": self.yes_price, "no": self.no_price},
            "order_book": {
                "yes": {
                    "bids": [[lv.price, lv.size] for lv in self.yes_book.bids],
                    "asks": [[lv.price, lv.size] for lv in self.yes_book.asks],
                },
                "no": {
                    "bids": [[lv.price, lv.size] for lv in self.no_book.bids],
                    "asks": [[lv.price, lv.size] for lv in self.no_book.asks],
                },
            },
            "best_bid_ask": {
                "yes": [self.yes_book.best_bid, self.yes_book.best_ask],
                "no": [self.no_book.best_bid, self.no_book.best_ask],
            },
            "available_depth_usdc": {
                "yes_asks": self.yes_book.ask_depth_usdc,
                "no_asks": self.no_book.ask_depth_usdc,
            },
            "liquidity_usdc": self.liquidity_usdc,
            "volume_24h_usdc": self.volume_24h_usdc,
            "price_change_1h": self.price_change_1h,
            "price_change_24h": self.price_change_24h,
            "recent_prices": self.recent_prices,
            "operator_context": self.operator_context,
            "captured_at": self.captured_at.isoformat(),
            # Остальные рынки матча: ставить можно на любой, указав его market_id
            "other_markets": [
                {
                    "market_id": s.market_id,
                    "question": s.question,
                    "type": s.market_type,
                    "outcome_labels": {"YES": s.yes_label, "NO": s.no_label},
                    "prices": {"yes": s.yes_price, "no": s.no_price},
                    "best_ask": {"yes": s.yes_best_ask, "no": s.no_best_ask},
                    "liquidity_usdc": s.liquidity_usdc,
                }
                for s in self.sibling_markets
            ],
        }
