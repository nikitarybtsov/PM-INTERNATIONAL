"""Тесты адаптеров: провайдеры данных и участники (mock, retry, timeout, валидация)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.market_data import MarketNotAvailable, MockMarketDataProvider
from app.adapters.market_data.polymarket import PolymarketDataProvider
from app.adapters.participants.base import ParticipantError, PortfolioView
from app.adapters.participants.claude import ClaudeAdapter
from app.adapters.participants.codex import CodexAdapter
from app.adapters.participants.titan import TitanManualAdapter
from app.constants import Action, Phase
from app.schemas.decision import TradeDecisionInput
from app.schemas.snapshot import BookLevel, MarketSnapshot, SnapshotBook, SnapshotMarketInfo

VIEW = PortfolioView(
    participant_key="codex",
    cash_balance=1000.0,
    reserved_balance=0.0,
    initial_balance=1000.0,
)


def demo_snapshot(snapshot_id: int = 1) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id=snapshot_id,
        phase=Phase.PREMATCH,
        market=SnapshotMarketInfo(
            market_id=1, external_id="m1", source="mock", title="A vs B", team_a="A", team_b="B"
        ),
        yes_price=0.5,
        no_price=0.5,
        yes_book=SnapshotBook(
            bids=[BookLevel(price=0.49, size=2000)], asks=[BookLevel(price=0.51, size=2000)]
        ),
        no_book=SnapshotBook(
            bids=[BookLevel(price=0.49, size=2000)], asks=[BookLevel(price=0.51, size=2000)]
        ),
        liquidity_usdc=5000.0,
    )


# --- провайдер рыночных данных ---------------------------------------------
def test_mock_provider_returns_demo_markets():
    provider = MockMarketDataProvider()
    markets = provider.search_markets("Dota")
    assert len(markets) >= 3
    assert all(m.source == "mock" for m in markets)
    assert all(m.starts_at is not None for m in markets)


def test_mock_provider_quote_has_book_and_liquidity():
    provider = MockMarketDataProvider()
    ref = provider.search_markets()[0]
    quote = provider.get_quote(ref)
    assert 0 < quote.yes_price < 1
    assert quote.yes_asks and quote.no_asks
    assert quote.liquidity_usdc > 0
    assert quote.yes_price + quote.no_price == pytest.approx(1.0, abs=1e-4)


def test_mock_provider_reports_missing_market():
    provider = MockMarketDataProvider()
    with pytest.raises(MarketNotAvailable):
        provider.get_market("несуществующий")


def test_polymarket_adapter_normalizes_payload():
    """Адаптер разбирает ответ Gamma API без обращения к сети."""
    raw_market = {
        "id": "12345",
        "slug": "spirit-vs-falcons",
        "question": "Team Spirit vs Falcons — The International",
        "outcomes": json.dumps(["Team Spirit", "Falcons"]),
        "outcomePrices": json.dumps(["0.58", "0.42"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "gameStartTime": "2026-10-12T15:00:00Z",
        "liquidityNum": 24000,
        "volume24hr": 90000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/markets"):
            return httpx.Response(200, json=raw_market)
        if request.url.path == "/book":
            token = request.url.params.get("token_id")
            price = 0.58 if token == "tok-yes" else 0.42
            return httpx.Response(
                200,
                json={
                    "bids": [{"price": str(price - 0.01), "size": "500"}],
                    "asks": [{"price": str(price), "size": "500"}],
                },
            )
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client
    )

    ref = provider.get_market("12345")
    assert ref.team_a == "Team Spirit"
    assert ref.team_b == "Falcons"
    assert ref.starts_at is not None

    quote = provider.get_quote(ref)
    assert quote.yes_price == pytest.approx(0.58)
    assert quote.yes_asks and quote.no_asks
    assert quote.liquidity_usdc == pytest.approx(24000)


def test_polymarket_adapter_raises_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client
    )
    with pytest.raises(MarketNotAvailable):
        provider.get_market("12345")


def test_polymarket_provider_declares_read_only():
    assert PolymarketDataProvider.supports_live_orders is False


# --- участники: mock-режим --------------------------------------------------
def test_codex_and_claude_work_without_api_keys():
    for adapter in (CodexAdapter(), ClaudeAdapter()):
        assert adapter.is_mock is True
        result = adapter.decide(demo_snapshot(), VIEW)
        assert isinstance(result.decision, TradeDecisionInput)
        assert result.model_name.startswith("mock:")
        assert result.prompt_version


def test_mock_decisions_are_deterministic():
    a = CodexAdapter().decide(demo_snapshot(7), VIEW).decision
    b = CodexAdapter().decide(demo_snapshot(7), VIEW).decision
    assert a.model_dump() == b.model_dump()


def test_codex_and_claude_have_different_styles():
    codex = CodexAdapter().decide(demo_snapshot(3), VIEW).decision
    claude = ClaudeAdapter().decide(demo_snapshot(3), VIEW).decision
    assert codex.model_dump() != claude.model_dump()


def test_mock_respects_position_limit():
    result = CodexAdapter().decide(demo_snapshot(11), VIEW).decision
    assert result.stake_usdc <= VIEW.cash_balance * 0.10 + 1e-6


# --- участники: реальный режим (замоканный транспорт) ----------------------
def _patch_llm(monkeypatch, adapter, responses: list):
    """Подменить транспорт адаптера последовательностью ответов/исключений."""
    calls = {"n": 0}

    def fake_call(system_prompt: str, user_prompt: str):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        value = responses[index]
        if isinstance(value, Exception):
            raise value
        return value, "model-v1"

    monkeypatch.setattr(adapter, "_call_model", fake_call)
    return calls


VALID_JSON = json.dumps(
    {
        "action": "BUY_YES",
        "estimated_probability": 0.62,
        "stake_usdc": 40,
        "max_acceptable_price": 0.6,
        "confidence": 0.7,
        "short_reason": "рынок недооценивает фаворита",
        "key_factors": ["форма"],
        "risk_factors": ["замена"],
        "information_used": ["snapshot"],
    }
)


def test_llm_adapter_parses_valid_response(monkeypatch):
    adapter = CodexAdapter(mock=False)
    _patch_llm(monkeypatch, adapter, [VALID_JSON])
    result = adapter.decide(demo_snapshot(), VIEW)
    assert result.decision.action == Action.BUY_YES
    assert result.attempts == 1
    assert result.model_version == "model-v1"


def test_llm_adapter_retries_on_invalid_json(monkeypatch):
    adapter = ClaudeAdapter(mock=False)
    calls = _patch_llm(monkeypatch, adapter, ["не json вовсе", VALID_JSON])
    result = adapter.decide(demo_snapshot(), VIEW)
    assert result.attempts == 2
    assert calls["n"] == 2
    assert result.errors  # первая попытка зафиксирована


def test_llm_adapter_retries_on_schema_violation(monkeypatch):
    bad = json.dumps(
        {
            "action": "BUY_YES",
            "estimated_probability": 5.0,  # вне диапазона
            "stake_usdc": 10,
            "max_acceptable_price": 0.5,
            "confidence": 0.5,
            "short_reason": "плохая схема",
        }
    )
    adapter = CodexAdapter(mock=False)
    _patch_llm(monkeypatch, adapter, [bad, VALID_JSON])
    result = adapter.decide(demo_snapshot(), VIEW)
    assert result.attempts == 2


def test_llm_adapter_gives_up_after_retries(monkeypatch):
    adapter = CodexAdapter(mock=False)
    _patch_llm(monkeypatch, adapter, ["мусор", "мусор", "мусор", "мусор"])
    with pytest.raises(ParticipantError) as exc:
        adapter.decide(demo_snapshot(), VIEW)
    assert exc.value.attempts >= 2


def test_llm_adapter_handles_timeout(monkeypatch):
    adapter = ClaudeAdapter(mock=False)
    _patch_llm(monkeypatch, adapter, [httpx.TimeoutException("timeout")])
    with pytest.raises(ParticipantError):
        adapter.decide(demo_snapshot(), VIEW)


# --- Titan ------------------------------------------------------------------
def test_titan_adapter_requires_manual_input():
    with pytest.raises(ParticipantError) as exc:
        TitanManualAdapter().decide(demo_snapshot(), VIEW)
    assert exc.value.error_type == "manual_input_required"


def test_titan_wrap_manual_produces_result():
    decision = TradeDecisionInput(
        action=Action.HOLD,
        estimated_probability=0.5,
        stake_usdc=0,
        confidence=0.6,
        short_reason="пропускаю",
    )
    result = TitanManualAdapter.wrap_manual(decision)
    assert result.model_name == "human:titan"
    assert result.decision.action == Action.HOLD
