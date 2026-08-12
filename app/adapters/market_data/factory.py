"""Выбор провайдера рыночных данных по конфигурации."""

from __future__ import annotations

import logging

from app.adapters.market_data.base import MarketDataProvider
from app.adapters.market_data.mock import MockMarketDataProvider
from app.adapters.market_data.polymarket import PolymarketDataProvider
from app.config import get_settings

logger = logging.getLogger(__name__)

_override: MarketDataProvider | None = None


def set_provider_override(provider: MarketDataProvider | None) -> None:
    """Подмена провайдера в тестах."""
    global _override
    _override = provider


def get_market_data_provider(raw_sink=None) -> MarketDataProvider:
    if _override is not None:
        return _override
    settings = get_settings()
    if settings.market_data_provider == "polymarket":
        return PolymarketDataProvider(raw_sink=raw_sink)
    return MockMarketDataProvider()
