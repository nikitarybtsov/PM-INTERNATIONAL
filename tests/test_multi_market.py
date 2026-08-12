"""Мультирыночная аналитика: участник ставит не только на исход серии.

У матча на Polymarket 20-30 рынков: победители карт, тоталы, форы, экзотика.
Недооценённым чаще оказывается не исход серии, а второстепенный рынок с тонким
стаканом — туда меньше смотрят, и цена дольше остаётся кривой.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.market_data.polymarket import PolymarketDataProvider
from app.constants import Action, Phase
from app.schemas.decision import TradeDecisionInput
from app.schemas.snapshot import (
    BookLevel,
    MarketSnapshot,
    SiblingMarket,
    SnapshotBook,
    SnapshotMarketInfo,
)


def snapshot_with_siblings() -> MarketSnapshot:
    book = SnapshotBook(
        bids=[BookLevel(price=0.68, size=2000)],
        asks=[BookLevel(price=0.70, size=2000)],
    )
    return MarketSnapshot(
        snapshot_id=1,
        phase=Phase.PREMATCH,
        market=SnapshotMarketInfo(
            market_id=10, external_id="main", source="polymarket",
            title="Spirit vs Xtreme (BO3)", team_a="Spirit", team_b="Xtreme",
        ),
        yes_price=0.70,
        no_price=0.30,
        yes_book=book,
        no_book=book,
        liquidity_usdc=19000,
        sibling_markets=[
            SiblingMarket(
                market_id=11, external_id="totals", question="Games Total: O/U 2.5",
                market_type="TOTALS", yes_label="Over", no_label="Under",
                yes_price=0.455, no_price=0.545, liquidity_usdc=12601,
            ),
            SiblingMarket(
                market_id=12, external_id="handicap",
                question="Game Handicap: TS (-1.5)", market_type="HANDICAP",
                yes_price=0.40, no_price=0.60, liquidity_usdc=5578,
            ),
        ],
    )


# --- фазы -------------------------------------------------------------------
def test_after_draft_phase_exists():
    assert Phase.AFTER_DRAFT.value == "AFTER_DRAFT"
    assert [p.value for p in Phase] == ["PREMATCH", "AFTER_DRAFT", "BETWEEN_MAPS"]


def test_after_draft_hint_mentions_picks():
    from app.adapters.participants.prompting import _PHASE_HINTS

    hint = _PHASE_HINTS["AFTER_DRAFT"]
    assert "пики" in hint.lower()
    assert "карта ещё не началась" in hint.lower()


# --- снимок несёт все рынки матча -------------------------------------------
def test_snapshot_exposes_sibling_markets_to_prompt():
    payload = snapshot_with_siblings().to_prompt_dict()

    others = payload["other_markets"]
    assert len(others) == 2
    assert {o["market_id"] for o in others} == {11, 12}
    totals = next(o for o in others if o["market_id"] == 11)
    assert totals["type"] == "TOTALS"
    assert totals["outcome_labels"] == {"YES": "Over", "NO": "Under"}
    assert totals["liquidity_usdc"] == 12601


def test_price_is_taken_from_chosen_market():
    """Оценку участника нельзя сравнивать с ценой чужого рынка."""
    snapshot = snapshot_with_siblings()

    assert snapshot.market_probability("YES") == pytest.approx(0.70)
    assert snapshot.market_probability("YES", 11) == pytest.approx(0.455)
    assert snapshot.market_probability("NO", 11) == pytest.approx(0.545)
    assert snapshot.market_probability("YES", 12) == pytest.approx(0.40)


def test_unknown_market_is_rejected():
    snapshot = snapshot_with_siblings()

    assert snapshot.allows_market(None) is True
    assert snapshot.allows_market(10) is True
    assert snapshot.allows_market(11) is True
    assert snapshot.allows_market(999) is False
    assert snapshot.tradeable_market_ids() == [10, 11, 12]


def test_sibling_markets_are_part_of_snapshot_hash():
    """Все участники должны видеть один и тот же набор рынков."""
    base = snapshot_with_siblings()
    without = base.model_copy(update={"sibling_markets": []})
    assert base.content_hash() != without.content_hash()


# --- схема решения ----------------------------------------------------------
def test_decision_can_target_another_market():
    decision = TradeDecisionInput(
        action=Action.BUY_YES,
        target_market_id=11,
        estimated_probability=0.52,
        stake_usdc=50,
        max_acceptable_price=0.47,
        confidence=0.6,
        short_reason="тотал недооценён: обе команды играют быстро",
    )
    assert decision.target_market_id == 11


def test_decision_defaults_to_main_market():
    decision = TradeDecisionInput(
        action=Action.HOLD, estimated_probability=0.5, stake_usdc=0,
        confidence=0.3, short_reason="жду данных",
    )
    assert decision.target_market_id is None


def test_prompt_schema_documents_market_choice():
    from app.schemas.decision import json_schema_for_prompt

    schema = json_schema_for_prompt()
    assert "target_market_id" in schema
    assert "other_markets" in schema


def test_system_prompt_tells_participant_to_pick_market():
    from app.adapters.participants.prompting import system_prompt

    text = system_prompt()
    assert "target_market_id" in text
    assert "ликвидность" in text.lower()


# --- провайдер отдаёт рынки события -----------------------------------------
def test_polymarket_lists_event_markets():
    event = {
        "title": "Dota 2: Spirit vs Xtreme (BO3) - The International",
        "slug": "dota2-ts8-xtreme",
        "markets": [
            {"id": "1", "question": "Spirit vs Xtreme (BO3) - The International",
             "outcomes": json.dumps(["Spirit", "Xtreme"]),
             "outcomePrices": json.dumps(["0.7", "0.3"]), "closed": False, "active": True},
            {"id": "2", "question": "Games Total: O/U 2.5",
             "outcomes": json.dumps(["Over", "Under"]),
             "outcomePrices": json.dumps(["0.455", "0.545"]), "closed": False, "active": True},
            {"id": "3", "question": "Game 1 Winner",
             "outcomes": json.dumps(["Spirit", "Xtreme"]),
             "outcomePrices": json.dumps(["0.62", "0.38"]), "closed": True, "active": True},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            assert request.url.params.get("slug") == "dota2-ts8-xtreme"
            return httpx.Response(200, json=[event])
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client
    )
    main = provider._normalize(event["markets"][0], event=event)

    siblings = provider.list_event_markets(main)

    ids = {s.external_id for s in siblings}
    assert "2" in ids           # тотал попал
    assert "1" not in ids       # сам рынок раунда исключён
    assert "3" not in ids       # закрытый рынок исключён


def test_sibling_market_is_not_executed_on_wrong_book():
    """Ставка на тотал не должна исполняться по стакану победителя серии.

    Снимок несёт стакан только основного рынка. Если исполнить по нему заявку
    на тотал, участник купит не то, что просил, и по другой цене — в боевом
    режиме за реальные деньги. Пока стакан выбранного рынка не подтягивается,
    такие заявки отклоняются.
    """
    from app.constants import RiskVerdict
    from app.services.risk_engine import RiskContext, evaluate

    snapshot = snapshot_with_siblings()
    ctx = RiskContext(
        action=Action.BUY_YES,
        stake_usdc=50.0,
        max_acceptable_price=0.47,
        snapshot=snapshot,
        cash_balance=1000.0,
        available_balance=1000.0,
        total_exposure=0.0,
        market_exposure=0.0,
        target_market_id=11,  # тотал O/U 2.5, стакана в снимке нет
    )

    outcome = evaluate(ctx)

    assert outcome.verdict is RiskVerdict.REJECTED
    codes = [r.code for r in outcome.reasons]
    assert "market_not_executable" in codes
    reason = next(r for r in outcome.reasons if r.code == "market_not_executable")
    assert "Games Total" in reason.message


def test_main_market_still_executes_normally():
    """Запрет касается только соседних рынков — основной работает как раньше."""
    from app.constants import RiskVerdict
    from app.services.risk_engine import RiskContext, evaluate

    ctx = RiskContext(
        action=Action.BUY_YES,
        stake_usdc=50.0,
        max_acceptable_price=0.75,
        snapshot=snapshot_with_siblings(),
        cash_balance=1000.0,
        available_balance=1000.0,
        total_exposure=0.0,
        market_exposure=0.0,
        target_market_id=None,
    )

    assert evaluate(ctx).verdict in (RiskVerdict.APPROVED, RiskVerdict.ADJUSTED)


def test_sibling_lookup_survives_provider_failure():
    """Сбой на соседних рынках не должен ронять снимок."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    provider = PolymarketDataProvider(
        gamma_url="http://test", clob_url="http://test", client=client
    )
    ref = provider._normalize({"id": "1", "slug": "x", "question": "q"})

    assert provider.list_event_markets(ref) == []
