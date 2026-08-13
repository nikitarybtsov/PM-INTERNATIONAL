"""Страховочные тесты: предохранители боевого режима и защита секретов.

Раньше здесь запрещалась реальная торговля как таковая. Теперь она разрешена
оператором, поэтому контракт безопасности сместился: проверяем, что боевой
режим не включается сам, что ордер не уходит без одобрения человека и что
приватные ключи не попадают в БД, логи, экспорт и репозиторий.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings, reset_settings_cache
from app.constants import Phase
from app.db.models import Market
from app.services import audit
from app.services import rounds as rounds_service

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"

FAKE_KEY = "0x" + "ab" * 32


# --- значения по умолчанию безопасны ----------------------------------------
def test_live_trading_is_off_by_default():
    """Чистая установка не торгует реальными деньгами."""
    settings = get_settings()
    assert settings.live_trading_enabled is False
    assert settings.execution_dry_run is True
    assert settings.live_execution_ready() is False


def test_env_example_keeps_live_trading_disabled():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("LIVE_TRADING_ENABLED="):
            assert stripped.split("=", 1)[1].strip().lower() in ("false", "0", "")
        if stripped.startswith("EXECUTION_DRY_RUN="):
            assert stripped.split("=", 1)[1].strip().lower() in ("true", "1", "")


def test_enabling_flag_alone_does_not_enable_live(monkeypatch):
    """Одного LIVE_TRADING_ENABLED мало: dry-run остаётся включён."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    reset_settings_cache()
    assert get_settings().live_execution_ready() is False
    reset_settings_cache()


def test_live_needs_wallets_for_both_participants(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    monkeypatch.setenv("CODEX_POLY_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.delenv("CLAUDE_POLY_PRIVATE_KEY", raising=False)
    reset_settings_cache()
    assert get_settings().live_execution_ready() is False
    reset_settings_cache()


# --- ордер не уходит без человека -------------------------------------------
def test_order_requires_operator_approval(monkeypatch):
    from app.adapters.execution import NotApproved, OrderRequest, PolymarketLiveAdapter

    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    monkeypatch.setenv("CODEX_POLY_PRIVATE_KEY", FAKE_KEY)
    reset_settings_cache()

    sent: list = []

    class Spy:
        def create_and_post_market_order(self, **kwargs):
            sent.append(kwargs)
            return {"success": True}

    adapter = PolymarketLiveAdapter(client_factory=lambda w: Spy())
    request = OrderRequest(
        participant="codex", market_id=1, token_id="t", outcome="YES",
        size=100, max_price=0.7, approved_by=None,
    )
    try:
        adapter.execute(request)
        raise AssertionError("ордер ушёл без одобрения оператора")
    except NotApproved:
        pass
    assert sent == [], "к бирже обратились несмотря на отсутствие одобрения"
    reset_settings_cache()


# --- источники данных остаются read-only ------------------------------------
def test_market_data_providers_declare_no_order_support():
    from app.adapters.market_data.mock import MockMarketDataProvider
    from app.adapters.market_data.polymarket import PolymarketDataProvider

    for provider_cls in (MockMarketDataProvider, PolymarketDataProvider):
        assert provider_cls.supports_live_orders is False
        assert not hasattr(provider_cls, "place_order")
        assert not hasattr(provider_cls, "submit_order")


def test_paper_engine_never_reaches_network():
    """Бумажный движок остаётся источником истины и в сеть не ходит."""
    text = (APP_DIR / "services" / "paper_engine.py").read_text(encoding="utf-8")
    for pattern in (r"\bhttpx\b", r"\brequests\b", r"ClobClient", r"post_order"):
        assert not re.search(pattern, text), f"paper_engine получил сетевой код: {pattern}"


def test_private_keys_confined_to_execution_layer():
    """Ключи читаются только конфигом и адаптером исполнения."""
    # Единственные места, где приватный ключ вообще упоминается: конфигурация,
    # которая его читает из окружения, и слой исполнения, который им подписывает.
    # Раньше здесь стояли пути app/adapters/execution/*, которых не существовало —
    # тест «проходил», охраняя пустоту.
    allowed = {
        Path("app/config.py"),
        Path("app/adapters/execution/polymarket_live.py"),
        Path("app/adapters/execution/base.py"),
    }
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"private_key|POLY_PRIVATE_KEY", text, flags=re.IGNORECASE):
            offenders.append(str(rel))
    assert not offenders, f"приватный ключ утёк за пределы слоя исполнения: {offenders}"


# --- секреты не покидают процесс --------------------------------------------
def test_env_example_has_no_real_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "sk-ant" not in text
    # приватный ключ — 0x + 64 hex; в примере его быть не должно
    assert not re.search(r"0x[a-fA-F0-9]{64}", text)
    for line in text.splitlines():
        if ("API_KEY" in line or "PRIVATE_KEY" in line) and "=" in line:
            assert line.split("=", 1)[1].strip() == "", f"в .env.example есть значение: {line}"


def test_gitignore_excludes_env_and_db():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.db", "*.key"):
        assert pattern in text


def test_audit_scrubs_secrets():
    scrubbed = audit.scrub(
        {
            "openai_api_key": "sk-secret",
            "private_key": FAKE_KEY,
            "nested": {"authorization": "Bearer x"},
            "ok": 1,
        }
    )
    assert scrubbed["openai_api_key"] == "***redacted***"
    assert scrubbed["private_key"] == "***redacted***"
    assert scrubbed["nested"]["authorization"] == "***redacted***"
    assert scrubbed["ok"] == 1


def test_no_secrets_persisted_in_database(db: Session, seeded, market: Market, monkeypatch):
    """Даже при заданных ключах в окружении они не попадают в БД."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-be-stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-be-stored")
    monkeypatch.setenv("CODEX_POLY_PRIVATE_KEY", FAKE_KEY)
    reset_settings_cache()

    from app.adapters.participants.claude import ClaudeAdapter
    from app.adapters.participants.codex import CodexAdapter
    from app.adapters.participants.factory import set_adapter_override

    set_adapter_override("codex", CodexAdapter(mock=True))
    set_adapter_override("claude", ClaudeAdapter(mock=True))

    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    db.commit()

    from sqlalchemy import text as sql_text

    from app.db.base import get_engine

    with get_engine().connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                sql_text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        ]
        for table in tables:
            rows = conn.execute(sql_text(f"SELECT * FROM {table}")).fetchall()  # noqa: S608
            blob = " ".join(str(cell) for row in rows for cell in row)
            assert "sk-test-should-never-be-stored" not in blob
            assert "sk-ant-should-never-be-stored" not in blob
            assert FAKE_KEY not in blob

    reset_settings_cache()


def test_settings_safe_dump_masks_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("CODEX_POLY_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.setenv("CODEX_POLY_FUNDER", "0x1111111111111111111111111111111111111111")
    reset_settings_cache()

    dumped = get_settings().safe_dump()

    assert dumped["openai_api_key"] == "***set***"
    assert "sk-secret-value" not in str(dumped)
    assert FAKE_KEY not in str(dumped)
    # публичный адрес кошелька показывать можно и нужно — по нему сверяют аккаунт
    assert dumped["wallets"]["codex"]["funder"].startswith("0x1111")
    assert dumped["wallets"]["codex"]["private_key"] == "***set***"
    reset_settings_cache()
