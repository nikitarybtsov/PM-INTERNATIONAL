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

# v2 — в промпт добавлена комиссия тейкера и порог безубыточности.
# Версия участвует в аудите: решения, принятые по разным промптам, несравнимы.
PROMPT_VERSION = "v2"

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
    # Тег Dota 2 в Gamma. Поиск идёт по нему, а не перебором активных рынков:
    # на Polymarket тысячи рынков и Dota 2 в первые N просто не попадает.
    polymarket_gamma_tag_id: int = 102366
    polymarket_event_limit: int = 100
    # У матча ~20-30 рынков (карты, форы, тоталы). По умолчанию берём только
    # основной рынок серии — эксперимент про победителя матча.
    polymarket_main_market_only: bool = True
    polymarket_only_upcoming: bool = True
    # История цены YES для поля recent_prices снимка. Без неё участники не
    # видят, двигался ли рынок перед матчем.
    polymarket_history_interval: str = "1d"
    polymarket_history_fidelity: int = 60
    polymarket_history_points: int = 12

    # --- Комиссия Polymarket (sports_fees_v2) ------------------------------
    # fee = C × rate × p × (1 − p); платит только тейкер, берётся при сделке.
    # Значение по умолчанию сверено с живым feeSchedule рынков The International.
    polymarket_taker_fee_rate: float = 0.05
    # Тир Taker Rebate Program. Bronze (3%) начинается с $2 000 weighted volume
    # за 30 дней — на банке $1 000 недостижимо, поэтому по умолчанию 0.
    polymarket_taker_rebate_rate: float = 0.0
    # Ограничения биржи: меньше минимума ордер отклоняется, цена кратна тику.
    polymarket_min_order_usdc: float = 5.0
    polymarket_price_tick: float = 0.01

    # участники (ключи только из окружения)
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str = "gpt-5.1"
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str | None = Field(default=None, repr=False)
    anthropic_model: str = "claude-opus-5"
    anthropic_base_url: str = "https://api.anthropic.com"

    # Claude через локальный CLI вместо HTTP API (выбор оператора).
    # Требует установленного и авторизованного `claude` на сервере.
    claude_transport: Literal["api", "cli"] = "api"
    claude_cli_command: str = "claude -p"
    claude_cli_timeout_seconds: float = 180.0
    claude_cli_model_label: str = "claude-cli"

    # Codex через локальный CLI вместо HTTP API (выбор оператора).
    # Требует установленного и авторизованного `codex` на сервере.
    codex_transport: Literal["api", "cli"] = "api"
    codex_cli_command: str = "codex exec --skip-git-repo-check"
    codex_cli_timeout_seconds: float = 180.0
    codex_cli_model_label: str = "codex-cli"

    participant_timeout_seconds: float = 60.0
    participant_max_retries: int = 2

    initial_bankroll_usdc: float = 1000.0

    paper_fee_bps: int = 0
    paper_allow_partial_fill: bool = True

    export_dir: str = "./exports"

    # --- Telegram --------------------------------------------------------
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = None
    telegram_timeout_seconds: float = 15.0

    # --- Доступ к панели --------------------------------------------------
    panel_user: str = "operator"
    panel_password: str | None = Field(default=None, repr=False)
    titan_access_token: str | None = Field(default=None, repr=False)
    public_base_url: str = "http://localhost:8000"

    # --- Автоматический планировщик раундов -------------------------------
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 300
    scheduler_open_before_match_minutes: int = 240
    scheduler_min_before_match_minutes: int = 20
    scheduler_max_open_rounds: int = 3
    scheduler_market_query: str = "Dota"
    # Что делать, если Титан не успел подать решение до дедлайна.
    # cancel — раунд отменяется, статистика не портится (безопасно по умолчанию)
    # hold   — за Титана записывается HOLD, раунд исполняется
    titan_timeout_policy: Literal["cancel", "hold"] = "cancel"
    titan_deadline_minutes: int = 15

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

    def has_telegram(self) -> bool:
        return bool(
            self.telegram_bot_token
            and self.telegram_bot_token.strip()
            and self.telegram_chat_id
            and str(self.telegram_chat_id).strip()
        )

    def panel_auth_enabled(self) -> bool:
        return bool(self.panel_password and self.panel_password.strip())

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
