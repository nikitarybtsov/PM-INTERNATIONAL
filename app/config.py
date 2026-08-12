"""Конфигурация приложения.

Все настройки читаются из переменных окружения (или `.env`).
Секреты НИКОГДА не сохраняются в БД, логах и экспортах — см. `safe_dump()`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# ЖЁСТКАЯ БЛОКИРОВКА РЕАЛЬНОЙ ТОРГОВЛИ.
# Константа не читается из окружения намеренно: включение live-режима требует
# отдельного явного изменения кода и ревью. Тест `tests/test_no_live_trading.py`
# защищает это значение.
# ---------------------------------------------------------------------------
LIVE_TRADING_ENABLED: bool = False

PROMPT_VERSION = "v1"

ROOT_DIR = Path(__file__).resolve().parent.parent


class RiskLimits(BaseSettings):
    """Детерминированные лимиты риск-движка. Одинаковы для всех участников."""

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    max_position_pct: float = 0.10
    max_match_pct: float = 0.25
    max_total_exposure_pct: float = 0.50
    min_stake_usdc: float = 1.0
    min_liquidity_usdc: float = 50.0
    max_slippage_bps: int = 300
    snapshot_ttl_seconds: int = 900

    @field_validator("max_position_pct", "max_match_pct", "max_total_exposure_pct")
    @classmethod
    def _pct_range(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("доля банка должна быть в диапазоне (0, 1]")
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/pm.db"

    # источник рыночных данных
    market_data_provider: Literal["mock", "polymarket"] = "mock"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_timeout_seconds: float = 15.0
    polymarket_search_query: str = "Dota"

    # участники (ключи только из окружения)
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str = "gpt-5.1"
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str | None = Field(default=None, repr=False)
    anthropic_model: str = "claude-opus-4-5"
    anthropic_base_url: str = "https://api.anthropic.com"

    participant_timeout_seconds: float = 60.0
    participant_max_retries: int = 2

    initial_bankroll_usdc: float = 1000.0

    paper_fee_bps: int = 0
    paper_allow_partial_fill: bool = True

    export_dir: str = "./exports"

    @property
    def risk(self) -> RiskLimits:
        return RiskLimits()

    @property
    def live_trading_enabled(self) -> bool:
        """Всегда False: live execution физически отсутствует."""
        return LIVE_TRADING_ENABLED

    def has_openai(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.strip())

    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.strip())

    def safe_dump(self) -> dict[str, object]:
        """Дамп конфигурации без секретов — пригоден для логов и /health."""
        data = self.model_dump()
        for key in list(data):
            if "api_key" in key or "secret" in key or "token" in key:
                data[key] = "***set***" if data[key] else None
        data["live_trading_enabled"] = LIVE_TRADING_ENABLED
        data["risk"] = self.risk.model_dump()
        return data


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Используется в тестах после подмены переменных окружения."""
    get_settings.cache_clear()
