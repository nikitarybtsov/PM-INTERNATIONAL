"""Тесты адаптеров: провайдеры данных и участники (mock, retry, timeout, валидация)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.market_data import MarketNotAvailable, MockMarketDataProvider
from app.adapters.market_data.polymarket import (
    PolymarketDataProvider,
    classify_market_type,
)
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


def test_polymarket_quote_includes_price_history():
    """recent_prices требуется ТЗ и доходит до промпта — живой адаптер обязан его заполнять."""
    raw_market = {
        "id": "1",
        "question": "A vs B",
        "outcomes": json.dumps(["A", "B"]),
        "outcomePrices": json.dumps(["0.69", "0.31"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "liquidityNum": 16000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/book":
            return httpx.Response(200, json={
                "bids": [{"price": "0.68", "size": "500"}],
                "asks": [{"price": "0.69", "size": "500"}],
            })
        if request.url.path == "/prices-history":
            assert request.url.params.get("market") == "tok-yes"
            return httpx.Response(200, json={
                "history": [{"t": 1786467613 + i * 3600, "p": 0.70 + i * 0.001}
                            for i in range(30)]
            })
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client
    )
    quote = provider.get_quote(provider._normalize(raw_market))

    # хвост истории, обрезанный до POLYMARKET_HISTORY_POINTS
    assert len(quote.recent_prices) == 12
    assert quote.recent_prices[-1] == pytest.approx(0.729)


def test_polymarket_quote_survives_missing_price_history():
    """Недоступная история не должна мешать зафиксировать snapshot."""
    raw_market = {
        "id": "1",
        "question": "A vs B",
        "outcomes": json.dumps(["A", "B"]),
        "outcomePrices": json.dumps(["0.69", "0.31"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "liquidityNum": 16000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/book":
            return httpx.Response(200, json={
                "bids": [{"price": "0.68", "size": "500"}],
                "asks": [{"price": "0.69", "size": "500"}],
            })
        if request.url.path == "/prices-history":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client
    )
    quote = provider.get_quote(provider._normalize(raw_market))

    assert quote.recent_prices == []
    assert quote.yes_asks  # стакан при этом на месте


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


# --- поиск рынков по тегу Dota 2 -------------------------------------------
def _gamma_market(mid: str, question: str, start: str, **extra) -> dict:
    """Рынок в том виде, в каком его отдаёт Gamma внутри события."""
    payload = {
        "id": mid,
        "question": question,
        "outcomes": json.dumps(["Team Spirit", "Xtreme Gaming"]),
        "outcomePrices": json.dumps(["0.69", "0.31"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "gameStartTime": start,
        "closed": False,
        "active": True,
        "liquidityNum": 19389.53,
    }
    payload.update(extra)
    return payload


def _dota_events_fixture(start: str) -> list[dict]:
    """Событие TI с полным набором рынков — как на реальном Polymarket."""
    return [
        {
            "title": "Dota 2: Team Spirit vs Xtreme Gaming (BO3) - The International Group Stage",
            "slug": "dota2-ts8-xtreme-2026-08-13",
            "startDate": "2026-06-01T00:00:00Z",  # у событий Gamma оно в прошлом
            "markets": [
                _gamma_market(
                    "1",
                    "Dota 2: Team Spirit vs Xtreme Gaming (BO3) - The International Group Stage",
                    start,
                ),
                _gamma_market(
                    "2", "Dota 2: Team Spirit vs Xtreme Gaming - Game 1 Winner", start
                ),
                _gamma_market("3", "Games Total: O/U 2.5", start),
                _gamma_market(
                    "4", "Game Handicap: TS (-1.5) vs Xtreme Gaming (+1.5)", start
                ),
                _gamma_market("5", "Game 1: Ends in Daytime?", start),
                _gamma_market(
                    "6",
                    "Dota 2: Closed Match (BO3) - The International Group Stage",
                    start,
                    closed=True,
                ),
            ],
        }
    ]


def _events_provider(events: list[dict], **kwargs) -> PolymarketDataProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(200, json=events)
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    return PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client, **kwargs
    )


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=6)).isoformat()


def _past() -> str:
    return (datetime.now(UTC) - timedelta(hours=6)).isoformat()


def test_polymarket_search_returns_only_main_series_market():
    """У матча ~20-30 рынков; по умолчанию берём только победителя серии."""
    provider = _events_provider(_dota_events_fixture(_future()))
    refs = provider.search_markets("Dota")

    assert len(refs) == 1
    ref = refs[0]
    assert ref.external_id == "1"
    assert ref.market_type == "MATCH_WINNER"
    assert ref.team_a == "Team Spirit"
    assert ref.tournament == "The International Group Stage"
    assert ref.event_title is not None


def test_polymarket_search_queries_gamma_by_dota_tag():
    """Без tag_id Gamma отдаёт случайные рынки и Dota 2 в выдачу не попадает."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["tag_id"] = request.url.params.get("tag_id")
        seen["closed"] = request.url.params.get("closed")
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client, tag_id=102366
    )
    assert provider.search_markets("Dota") == []
    assert seen["path"] == "/events"
    assert seen["tag_id"] == "102366"
    assert seen["closed"] == "false"


def test_polymarket_search_skips_started_matches():
    provider = _events_provider(_dota_events_fixture(_past()))
    assert provider.search_markets("Dota") == []

    permissive = _events_provider(_dota_events_fixture(_past()), only_upcoming=False)
    assert len(permissive.search_markets("Dota")) == 1


def test_polymarket_search_can_include_submarkets():
    """main_market_only=false открывает карты, форы и тоталы — но не закрытые рынки."""
    provider = _events_provider(_dota_events_fixture(_future()), main_market_only=False)
    refs = provider.search_markets("Dota")

    ids = {r.external_id for r in refs}
    assert ids == {"1", "2", "3", "4", "5"}  # "6" закрыт у источника
    by_id = {r.external_id: r.market_type for r in refs}
    assert by_id["1"] == "MATCH_WINNER"
    assert by_id["2"] == "MAP_WINNER"
    assert by_id["3"] == "TOTALS"
    assert by_id["4"] == "HANDICAP"
    assert by_id["5"] == "SPECIAL"


def test_polymarket_search_filters_by_tournament_query():
    provider = _events_provider(_dota_events_fixture(_future()))
    assert len(provider.search_markets("The International")) == 1
    assert provider.search_markets("ESL One") == []


def test_classify_market_type_prefers_handicap_over_map():
    """«Game Handicap: TS (-1.5)…» содержит и game, и фору — это фора."""
    assert classify_market_type("Game Handicap: TS (-1.5) vs XG (+1.5)") == "HANDICAP"
    assert classify_market_type("Dota 2: A vs B - Game 2 Winner") == "MAP_WINNER"
    assert classify_market_type("Games Total: O/U 2.5") == "TOTALS"
    assert classify_market_type("Dota 2: A vs B (BO5) - TI Playoffs") == "MATCH_WINNER"
    assert classify_market_type("Match Winner") == "MATCH_WINNER"
    assert classify_market_type("Which Hero Will be Announced?") == "SPECIAL"
    # номер карты без «winner» — экзотика, иначе исказится статистика по типам
    assert classify_market_type("Game 1: Ends in Daytime?") == "SPECIAL"


def test_polymarket_parses_gamma_space_separated_datetime():
    """Gamma отдаёт `2026-08-13 05:00:00+00`, а не ISO с `T`."""
    # only_upcoming=False — дата фиксированная, тест не должен протухнуть.
    provider = _events_provider(
        _dota_events_fixture("2026-08-13 05:00:00+00"), only_upcoming=False
    )
    refs = provider.search_markets("Dota", limit=5)
    assert len(refs) == 1
    assert refs[0].starts_at == datetime(2026, 8, 13, 5, 0, tzinfo=UTC)


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


# --- пики из живой трансляции ----------------------------------------------
def test_draft_matches_teams_despite_name_differences(monkeypatch):
    """У Valve и Polymarket названия расходятся: «Nigma Galaxy » с пробелом."""
    from app.services import draft as draft_service

    live = [
        {
            "team_name_radiant": "Nigma Galaxy ",
            "team_name_dire": "Iron Wing",
            "game_time": 300,
            "delay": 10,
            "league_id": 19719,
            "players": (
                [{"hero_id": i, "team": 0} for i in range(1, 6)]
                + [{"hero_id": i, "team": 1} for i in range(6, 11)]
            ),
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/heroes"):
            return httpx.Response(
                200, json=[{"id": i, "localized_name": f"Hero{i}"} for i in range(1, 11)]
            )
        return httpx.Response(200, json=live)

    real_client = httpx.Client  # до подмены, иначе рекурсия
    monkeypatch.setattr(
        draft_service.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    draft_service._HERO_CACHE.clear()

    found = draft_service.fetch_draft("Nigma Galaxy", "Iron Wing")
    assert found is not None
    assert found.radiant_picks == [f"Hero{i}" for i in range(1, 6)]
    assert found.is_complete


def test_draft_prefers_the_newest_game_of_the_series(monkeypatch):
    """В эфире две карты серии: нужна та, чей драфт только закончился."""
    from app.services import draft as draft_service

    def game(seconds: int, offset: int) -> dict:
        return {
            "team_name_radiant": "BoomBoys",
            "team_name_dire": "OG",
            "game_time": seconds,
            "delay": 10,
            "players": (
                [{"hero_id": i + offset, "team": 0} for i in range(1, 6)]
                + [{"hero_id": i + offset, "team": 1} for i in range(6, 11)]
            ),
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/heroes"):
            return httpx.Response(
                200, json=[{"id": i, "localized_name": f"Hero{i}"} for i in range(1, 30)]
            )
        return httpx.Response(200, json=[game(2600, 0), game(220, 10)])

    real_client = httpx.Client  # до подмены, иначе рекурсия
    monkeypatch.setattr(
        draft_service.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    draft_service._HERO_CACHE.clear()

    found = draft_service.fetch_draft("BoomBoys", "OG")
    assert found.game_time == 220, "взята доигрывающаяся карта вместо новой"
    assert found.radiant_picks[0] == "Hero11"


def test_draft_returns_none_when_match_not_live(monkeypatch):
    from app.services import draft as draft_service

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/heroes"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    real_client = httpx.Client  # до подмены, иначе рекурсия
    monkeypatch.setattr(
        draft_service.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    draft_service._HERO_CACHE.clear()
    assert draft_service.fetch_draft("A", "B") is None
