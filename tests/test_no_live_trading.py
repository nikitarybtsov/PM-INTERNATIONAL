"""Страховочные тесты: живой торговли нет, секреты не утекают.

Эти тесты — часть контракта безопасности проекта. Если кто-то попытается
включить реальное исполнение или записать ключ в БД/лог, они упадут.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import LIVE_TRADING_ENABLED, get_settings
from app.constants import Phase
from app.db.models import Market
from app.services import audit
from app.services import rounds as rounds_service

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"

# признаки реального исполнения / подписи транзакций
FORBIDDEN_PATTERNS = [
    r"\bprivate_key\b",
    r"\beth_account\b",
    r"\bweb3\b",
    r"\bsign_transaction\b",
    r"\bsend_raw_transaction\b",
    r"\bplace_order\b",
    r"\bcreate_order\b",
    r"POLY_PRIVATE_KEY",
]


def test_live_trading_constant_is_false():
    assert LIVE_TRADING_ENABLED is False
    assert get_settings().live_trading_enabled is False


def test_no_order_placement_code_in_repository():
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                offenders.append(f"{path.relative_to(ROOT)}: {pattern}")
    assert not offenders, f"найдены следы реального исполнения: {offenders}"


def test_market_data_providers_declare_no_order_support():
    from app.adapters.market_data.mock import MockMarketDataProvider
    from app.adapters.market_data.polymarket import PolymarketDataProvider

    for provider_cls in (MockMarketDataProvider, PolymarketDataProvider):
        assert provider_cls.supports_live_orders is False
        assert not hasattr(provider_cls, "place_order")
        assert not hasattr(provider_cls, "submit_order")


def test_paper_engine_refuses_live_mode(monkeypatch, db: Session, seeded, market: Market):
    """Если константу когда-нибудь включат — движок обязан отказать."""
    from app.services import paper_engine

    monkeypatch.setattr(paper_engine, "LIVE_TRADING_ENABLED", True)
    with pytest.raises(paper_engine.LiveTradingForbidden):
        paper_engine.execute(
            db, decision=None, risk=None, snapshot_row=None, snapshot_model=None
        )


def test_env_example_has_no_real_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "sk-ant" not in text
    for line in text.splitlines():
        if "API_KEY" in line and "=" in line:
            assert line.split("=", 1)[1].strip() == "", f"в .env.example есть значение: {line}"


def test_gitignore_excludes_env_and_db():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.db", "*.key"):
        assert pattern in text


def test_audit_scrubs_secrets():
    scrubbed = audit.scrub(
        {"openai_api_key": "sk-secret", "nested": {"authorization": "Bearer x"}, "ok": 1}
    )
    assert scrubbed["openai_api_key"] == "***redacted***"
    assert scrubbed["nested"]["authorization"] == "***redacted***"
    assert scrubbed["ok"] == 1


def test_no_api_keys_persisted_in_database(db: Session, seeded, market: Market, monkeypatch):
    """Даже при заданных ключах в окружении они не попадают в БД."""
    from app.config import reset_settings_cache

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-be-stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-be-stored")
    reset_settings_cache()

    # адаптеры в mock-режиме принудительно, чтобы не ходить в сеть
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

    reset_settings_cache()


def test_settings_safe_dump_masks_keys(monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    reset_settings_cache()
    dumped = get_settings().safe_dump()
    assert dumped["openai_api_key"] == "***set***"
    assert "sk-secret-value" not in str(dumped)
    reset_settings_cache()
