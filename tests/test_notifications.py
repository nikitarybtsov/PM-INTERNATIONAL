"""Тесты уведомлений: приватность до раскрытия, отказоустойчивость, Telegram."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import Session

from app.adapters.notifiers.base import Notifier, NullNotifier
from app.adapters.notifiers.factory import set_notifier_override
from app.adapters.notifiers.telegram import TelegramNotifier, _split
from app.constants import Phase
from app.db.models import Market, Participant
from app.schemas.decision import TradeDecisionInput
from app.services import notifications, settlement
from app.services import rounds as rounds_service

TITAN_BUY = TradeDecisionInput(
    action="BUY_YES",
    estimated_probability=0.7,
    stake_usdc=60,
    max_acceptable_price=0.95,
    confidence=0.8,
    short_reason="СЕКРЕТНОЕ ОБОСНОВАНИЕ ТИТАНА",
)


@pytest.fixture
def notifier() -> NullNotifier:
    sink = NullNotifier()
    set_notifier_override(sink)
    yield sink
    set_notifier_override(None)


def _ai_reasons(db: Session, round_id: int) -> list[str]:
    out = []
    for decision in rounds_service.decisions_for(db, round_id):
        reason = (decision.payload or {}).get("short_reason")
        if reason:
            out.append(reason)
    return out


# --- приватность ------------------------------------------------------------
def test_round_opened_contains_no_decisions(db: Session, seeded, market: Market, notifier):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)

    notifications.round_opened(db, round_row)
    text = notifier.messages[-1]

    assert f"#{round_row.id}" in text
    for reason in _ai_reasons(db, round_row.id):
        assert reason not in text


def test_ai_collected_reports_only_facts(db: Session, seeded, market: Market, notifier):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    stored = rounds_service.request_ai_decisions(db, round_row)
    statuses = {
        db.get(Participant, d.participant_id).key: d.status for d in stored
    }

    notifications.ai_collected(db, round_row, statuses)
    text = notifier.messages[-1]

    assert "codex" in text and "claude" in text
    assert "скрыто" in text
    for reason in _ai_reasons(db, round_row.id):
        assert reason not in text
    # числовые оценки тоже не должны утекать
    for decision in rounds_service.decisions_for(db, round_row.id):
        if decision.estimated_probability is not None:
            assert f"{decision.estimated_probability:.4f}" not in text


def test_titan_reminder_has_no_decisions(db: Session, seeded, market: Market, notifier):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)

    notifications.titan_reminder(round_row, 5)
    text = notifier.messages[-1]
    for reason in _ai_reasons(db, round_row.id):
        assert reason not in text


def test_round_executed_is_blocked_before_execution(db: Session, seeded, market: Market, notifier):
    """Даже прямой вызов раскрытия не сработает, пока раунд не исполнен."""
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)

    sent = notifications.round_executed(db, round_row, {})
    assert sent is False
    assert notifier.messages == []


def test_round_executed_reveals_after_execution(db: Session, seeded, market: Market, notifier):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    report = rounds_service.execute_round(db, round_row)

    assert notifications.round_executed(db, round_row, report) is True
    text = notifier.messages[-1]
    assert "СЕКРЕТНОЕ ОБОСНОВАНИЕ ТИТАНА" in text
    assert "CODEX" in text and "CLAUDE" in text


def test_settlement_notification(db: Session, seeded, market: Market, notifier):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    rounds_service.execute_round(db, round_row)
    settlement.settle_market(db, market, "YES")

    notifications.market_settled(db, market, "YES")
    text = notifier.messages[-1]
    assert "Победил" in text
    assert "titan" in text


def test_daily_digest(db: Session, seeded, market: Market, notifier):
    notifications.daily_digest(db)
    text = notifier.messages[-1]
    assert "Скоборд" in text
    assert "Лучший трейдер" in text


# --- отказоустойчивость -----------------------------------------------------
def test_notification_failure_does_not_break_round(db: Session, seeded, market: Market):
    class BrokenNotifier(Notifier):
        name = "broken"

        def send(self, text: str) -> bool:
            raise RuntimeError("Telegram недоступен")

    set_notifier_override(BrokenNotifier())
    try:
        round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
        assert notifications.round_opened(db, round_row) is False
        # раунд продолжает работать
        rounds_service.request_ai_decisions(db, round_row)
        rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
        report = rounds_service.execute_round(db, round_row)
        assert set(report) == {"codex", "claude", "titan"}
    finally:
        set_notifier_override(None)


# --- Telegram-транспорт -----------------------------------------------------
def test_telegram_disabled_without_config():
    assert TelegramNotifier(token=None, chat_id=None).enabled is False


def test_telegram_sends_message():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(token="tok", chat_id="123", client=client)
    assert notifier.send("привет") is True
    assert "/bottok/sendMessage" in seen["url"]
    assert "123" in seen["body"]


def test_telegram_survives_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "flood"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(token="tok", chat_id="1", client=client)
    assert notifier.send("текст") is False


def test_telegram_survives_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(token="tok", chat_id="1", client=client)
    assert notifier.send("текст") is False


def test_long_message_is_split():
    chunks = _split("строка\n" * 2000)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)


def test_telegram_token_never_in_message_body():
    """Токен уходит только в URL, но не в тело и не в логи."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    TelegramNotifier(token="SECRET-TOKEN", chat_id="1", client=client).send("тест")
    assert "SECRET-TOKEN" not in seen["body"]
