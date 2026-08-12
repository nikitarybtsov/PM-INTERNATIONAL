"""Конфигурация приложения.

Все настройки читаются из переменных окружения (или `.env`).
Секреты НИКОГДА не сохраняются в БД, логах и экспортах — см. `safe_dump()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# РЕАЛЬНАЯ ТОРГОВЛЯ.
#
# Раньше здесь стоял хардкод False. Теперь режим включается оператором, но
# одного флага недостаточно — предохранителей три, и снимать их нужно по
# отдельности:
#
#   1. LIVE_TRADING_ENABLED=true      — общее разрешение (по умолчанию выкл.);
#   2. EXECUTION_DRY_RUN=false        — иначе ордер только логируется, но не
#                                       уходит на биржу (по умолчанию вкл.);
#   3. одобрение оператора на КАЖДУЮ сделку — движок не исполняет ничего сам.
#
# Что защищено тестами (`tests/test_live_execution.py`, `test_no_live_trading.py`):
# значения по умолчанию безопасны, ключи не попадают в БД, логи и экспорт,
# без одобрения оператора сделка не уходит.
# ---------------------------------------------------------------------------

# v2 — в промпт добавлена комиссия тейкера и порог безубыточности.
# Версия участвует в аудите: решения, принятые по разным промптам, несравнимы.
PROMPT_VERSION = "v2"

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class WalletConfig:
    """Торговая связка участника. Приватный ключ живёт только в памяти процесса."""

    participant: str
    private_key: str
    funder: str | None
    signature_type: int
    chain_id: int = 137

    def __repr__(self) -> str:  # pragma: no cover - защита от случайного логирования
        return (
            f"WalletConfig(participant={self.participant!r}, funder={self.funder!r}, "
            f"signature_type={self.signature_type}, private_key=***)"
        )

    __str__ = __repr__


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

    # Стаканы соседних рынков матча. Каждый стоит двух запросов к бирже, а
    # рынков у матча 20-30 — брать все значит держать снимок десятки секунд.
    # Поэтому стаканы тянутся только для типов, на которых имеет смысл торговать,
    # и не больше лимита. Остальные рынки участник видит, но ставить не может.
    polymarket_sibling_book_types: str = "MATCH_WINNER,MAP_WINNER,TOTALS,HANDICAP"
    polymarket_sibling_book_limit: int = 8

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

    # --- Реальное исполнение на Polymarket ---------------------------------
    # Три независимых предохранителя, см. комментарий в начале файла.
    live_trading_enabled: bool = False
    execution_dry_run: bool = True
    # FAK — допускается частичное исполнение (рекомендуется для тейкера),
    # FOK — либо весь объём, либо отмена.
    execution_taker_order_type: Literal["FAK", "FOK"] = "FAK"
    execution_timeout_seconds: float = 30.0
    polymarket_chain_id: int = 137

    # Кошельки участников. Ключи только из окружения; в репозитории их нет.
    codex_poly_private_key: str | None = Field(default=None, repr=False)
    codex_poly_funder: str | None = None
    codex_poly_signature_type: int = 3
    claude_poly_private_key: str | None = Field(default=None, repr=False)
    claude_poly_funder: str | None = None
    claude_poly_signature_type: int = 1
    # Кошелёк Титана — только для чтения его сделок, ключ не нужен.
    titan_poly_address: str | None = None

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

    def wallet_for(self, participant_key: str) -> WalletConfig | None:
        """Торговая связка участника или None, если ключи не заданы."""
        prefix = participant_key.strip().lower()
        key = getattr(self, f"{prefix}_poly_private_key", None)
        funder = getattr(self, f"{prefix}_poly_funder", None)
        if not key or not str(key).strip():
            return None
        return WalletConfig(
            participant=prefix,
            private_key=str(key).strip(),
            funder=(str(funder).strip() or None) if funder else None,
            signature_type=int(getattr(self, f"{prefix}_poly_signature_type", 1)),
            chain_id=self.polymarket_chain_id,
        )

    def live_execution_ready(self) -> bool:
        """Можно ли вообще отправлять реальные ордера прямо сейчас."""
        return bool(
            self.live_trading_enabled
            and not self.execution_dry_run
            and self.wallet_for("codex")
            and self.wallet_for("claude")
        )

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

    #: Подстроки в имени поля, по которым значение считается секретом.
    #: Список именно расширяемый: добавляя новый секрет в настройки, добавьте
    #: сюда его признак — иначе значение уедет в /api/config и в логи.
    SECRET_NAME_PARTS: ClassVar[tuple[str, ...]] = (
        "api_key", "secret", "token", "password", "passphrase", "private_key",
    )

    def safe_dump(self) -> dict[str, object]:
        """Дамп конфигурации без секретов — пригоден для логов и /api/config."""
        data = self.model_dump()
        for key in list(data):
            if any(part in key.lower() for part in self.SECRET_NAME_PARTS):
                data[key] = "***set***" if data[key] else None
        data["live_trading_enabled"] = self.live_trading_enabled
        data["execution_dry_run"] = self.execution_dry_run
        data["live_execution_ready"] = self.live_execution_ready()
        # Кошельки: показываем только публичные адреса, ключи — никогда.
        data["wallets"] = {
            key: {
                "funder": w.funder,
                "signature_type": w.signature_type,
                "private_key": "***set***",
            }
            for key in ("codex", "claude")
            if (w := self.wallet_for(key))
        }
        data["risk"] = self.risk.model_dump()
        return data


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Используется в тестах после подмены переменных окружения."""
    get_settings.cache_clear()
