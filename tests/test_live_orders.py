"""Боевое исполнение: ордер обязан доходить до биржи.

Главный тест — `test_live_mode_actually_sends_order_to_exchange`. Он закрывает
реальную ошибку: адаптер исполнения (`app/adapters/execution/polymarket_live.py`)
был написан, покрыт тестами и настроен, но `execute_round` его никогда не
вызывал — исполнение всегда уходило в симулятор. Панель показывала «РЕАЛЬНЫЕ
ДЕНЬГИ», Telegram сообщал «ставки отправлены на биржу», ордер записывался как
FILLED, а на бирже его не было.

Тесты ниже проверяют не наличие модуля, а факт вызова: адаптер подменяется
и считает, сколько заявок через него прошло.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.adapters.execution import ExecutionError, OrderResult, OrderStatus
from app.config import reset_settings_cache
from app.constants import Phase
from app.db.models import Decision, Market
from app.services import rounds as rounds_service


class _FakeAdapter:
    """Подменяет биржу и запоминает, что через неё прошло."""

    def __init__(self, error: str | None = None) -> None:
        self.requests: list = []
        self.error = error

    def execute(self, request):
        self.requests.append(request)
        if self.error:
            raise ExecutionError(self.error)
        return OrderResult(
            status=OrderStatus.FILLED,
            filled_size=request.size,
            avg_price=request.max_price,
            order_id="0xLIVE",
        )

    def balance_usdc(self, participant: str) -> float | None:
        return 1000.0


def _make_bets(db: Session, round_row) -> None:
    """Превратить решения раунда в заявки на покупку и одобрить их."""
    for decision in rounds_service.decisions_for(db, round_row.id):
        decision.action = "BUY_YES"
        decision.stake_usdc = 50.0
        decision.max_acceptable_price = 0.95
        decision.payload = {
            **(decision.payload or {}),
            "action": "BUY_YES",
            "stake_usdc": 50.0,
            "max_acceptable_price": 0.95,
        }
        rounds_service.approve_decision(db, round_row, decision.participant.key)
    db.flush()


@pytest.fixture
def solo_ai(monkeypatch):
    """Титан торгует со своего кошелька — раунд его не ждёт."""
    monkeypatch.setenv("TITAN_PARTICIPATES_IN_ROUNDS", "false")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def live_mode(monkeypatch, solo_ai):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    monkeypatch.setenv("CODEX_POLY_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("CODEX_POLY_FUNDER", "0xCODEX")
    monkeypatch.setenv("CLAUDE_POLY_PRIVATE_KEY", "0x" + "22" * 32)
    monkeypatch.setenv("CLAUDE_POLY_FUNDER", "0xCLAUDE")
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_live_mode_actually_sends_order_to_exchange(
    db: Session, seeded, market: Market, live_mode, monkeypatch
):
    """В боевом режиме ордер обязан уйти на биржу, а не только в симулятор."""
    market.yes_token_id = "token-yes-123"
    market.no_token_id = "token-no-456"
    db.flush()

    fake = _FakeAdapter()
    monkeypatch.setattr(rounds_service, "get_execution_adapter", lambda: fake)

    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    _make_bets(db, round_row)
    rounds_service.execute_round(db, round_row)

    assert fake.requests, (
        "в боевом режиме ни одного ордера не ушло на биржу — "
        "исполнение осталось симуляцией"
    )
    sent = fake.requests[0]
    assert sent.token_id == "token-yes-123"
    assert sent.outcome == "YES"
    assert sent.approved_by, "на биржу ушла заявка без одобрения оператора"
    assert sent.idempotency_key, "нет ключа идемпотентности — возможен повторный ордер"

    stored = db.query(Decision).filter(Decision.live_order_id.is_not(None)).all()
    assert stored, "идентификатор биржевого ордера не сохранён"
    assert stored[0].live_order_id == "0xLIVE"


def test_live_order_failure_does_not_break_round(
    db: Session, seeded, market: Market, live_mode, monkeypatch
):
    """Отказ биржи фиксируется в отчёте, но раунд доводится до конца."""
    market.yes_token_id = "token-yes-123"
    db.flush()
    monkeypatch.setattr(
        rounds_service, "get_execution_adapter",
        lambda: _FakeAdapter(error="недостаточно средств"),
    )

    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    _make_bets(db, round_row)
    report = rounds_service.execute_round(db, round_row)

    assert any("недостаточно средств" in str(e.get("live_error", "")) for e in report.values())


def test_market_without_token_reports_clear_reason(
    db: Session, seeded, market: Market, live_mode, monkeypatch
):
    """Без token_id ордер отправить нельзя — причина должна быть понятной."""
    market.yes_token_id = None
    db.flush()
    fake = _FakeAdapter()
    monkeypatch.setattr(rounds_service, "get_execution_adapter", lambda: fake)

    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    _make_bets(db, round_row)
    report = rounds_service.execute_round(db, round_row)

    assert not fake.requests
    assert any("token_id" in str(e.get("live_error", "")) for e in report.values())


def test_paper_mode_never_touches_exchange(
    db: Session, seeded, market: Market, solo_ai, monkeypatch
):
    """Без боевого режима биржа не должна вызываться вообще."""
    market.yes_token_id = "token-yes-123"
    db.flush()
    fake = _FakeAdapter()
    monkeypatch.setattr(rounds_service, "get_execution_adapter", lambda: fake)

    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    db.flush()
    rounds_service.execute_round(db, round_row)

    assert fake.requests == [], "в бумажном режиме кто-то обратился к бирже"
