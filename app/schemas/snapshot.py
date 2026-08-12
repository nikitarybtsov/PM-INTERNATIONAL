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


class MarketSnapshot(BaseModel):
    """Полный снимок, который видят участники."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: int | None = None
    phase: Phase
    map_number: int | None = None
    market: SnapshotMarketInfo

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

    def book_for(self, outcome: str) -> SnapshotBook:
        return self.yes_book if outcome == "YES" else self.no_book

    def market_probability(self, outcome: str) -> float:
        return self.yes_price if outcome == "YES" else self.no_price

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
        }
