"""Тесты раздельности банков и отсутствия утечки решений между участниками."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.adapters.participants.prompting import build_prompt
from app.constants import Phase, RoundStatus
from app.db.models import Market
from app.services import portfolio as pf
from app.services import rounds as rounds_service


# --- раздельные банки -------------------------------------------------------
def test_three_independent_portfolios(db: Session, seeded):
    keys = {p.key for p in seeded["participants"]}
    assert keys == {"codex", "claude", "titan"}
    for participant in seeded["participants"]:
        portfolio = pf.get_portfolio(db, participant.id)
        assert portfolio.initial_balance == 1000.0
        assert portfolio.cash_balance == 1000.0


def test_debiting_one_bank_does_not_touch_others(db: Session, seeded):
    codex = rounds_service.get_participant(db, "codex")
    claude = rounds_service.get_participant(db, "claude")
    titan = rounds_service.get_participant(db, "titan")

    pf.debit(db, codex.id, 250.0)
    assert pf.get_portfolio(db, codex.id).cash_balance == pytest.approx(750.0)
    assert pf.get_portfolio(db, claude.id).cash_balance == pytest.approx(1000.0)
    assert pf.get_portfolio(db, titan.id).cash_balance == pytest.approx(1000.0)


def test_negative_balance_is_impossible(db: Session, seeded):
    codex = rounds_service.get_participant(db, "codex")
    with pytest.raises(pf.InsufficientFunds):
        pf.debit(db, codex.id, 1500.0)
    assert pf.get_portfolio(db, codex.id).cash_balance == pytest.approx(1000.0)


def test_reserve_cannot_exceed_available(db: Session, seeded):
    codex = rounds_service.get_participant(db, "codex")
    pf.reserve(db, codex.id, 600.0)
    with pytest.raises(pf.InsufficientFunds):
        pf.reserve(db, codex.id, 500.0)


def test_positions_are_isolated_per_participant(db: Session, seeded, market: Market):
    codex = rounds_service.get_participant(db, "codex")
    claude = rounds_service.get_participant(db, "claude")
    assert pf.open_positions(db, codex.id) == []
    assert pf.exposure(db, claude.id) == 0.0


# --- отсутствие утечки решений ---------------------------------------------
def test_prompt_contains_only_own_bank(db: Session, seeded, market: Market):
    """В промпте участника не должно быть ни имён, ни данных других участников."""
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    snapshot = rounds_service.snapshot_model_for(db, round_row)

    codex = rounds_service.get_participant(db, "codex")
    view = pf.portfolio_view(db, codex, snapshot)
    system, user = build_prompt(snapshot, view)

    lowered = (system + user).lower()
    assert "codex" in user.lower()  # свой ключ участник видит
    for foreign in ("claude", "titan"):
        assert foreign not in lowered, f"в промпте не должно быть упоминания {foreign}"


def test_prompts_are_identical_except_own_bank(db: Session, seeded, market: Market):
    """Codex и Claude получают один и тот же snapshot и одинаковые правила."""
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    snapshot = rounds_service.snapshot_model_for(db, round_row)

    codex = rounds_service.get_participant(db, "codex")
    claude = rounds_service.get_participant(db, "claude")
    sys_a, user_a = build_prompt(snapshot, pf.portfolio_view(db, codex, snapshot))
    sys_b, user_b = build_prompt(snapshot, pf.portfolio_view(db, claude, snapshot))

    assert sys_a == sys_b
    # различие только в строке "participant": "<key>"
    assert user_a.replace('"codex"', '"X"') == user_b.replace('"claude"', '"X"')


def test_decisions_hidden_until_round_executed(db: Session, seeded, market: Market, client):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    db.commit()

    # Codex и Claude ответили, Titan ещё нет
    response = client.post(f"/api/rounds/{round_row.id}/request-ai")
    assert response.status_code == 200
    body = response.json()
    assert set(body["collected"]) == {"codex", "claude"}
    # содержимое решений в ответе отсутствует
    assert "decision" not in str(body).lower() or "estimated_probability" not in str(body)

    hidden = client.get(f"/api/rounds/{round_row.id}/decisions").json()
    assert hidden["hidden"] is True
    assert hidden["decisions"] == []
    assert set(hidden["submitted"]) == {"codex", "claude"}
    assert hidden["awaiting"] == ["titan"]


def test_titan_page_does_not_contain_ai_decisions(db: Session, seeded, market: Market, client):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    db.commit()
    client.post(f"/api/rounds/{round_row.id}/request-ai")

    page = client.get(f"/ui/rounds/{round_row.id}/titan")
    assert page.status_code == 200
    html = page.text

    # достаём тексты обоснований ИИ и убеждаемся, что их нет на странице Титана
    decisions = rounds_service.decisions_for(db, round_row.id)
    assert decisions, "решения ИИ должны быть сохранены"
    for decision in decisions:
        reason = (decision.payload or {}).get("short_reason")
        if reason:
            assert reason not in html


def test_round_page_hides_decisions_before_execution(db: Session, seeded, market: Market, client):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    db.commit()
    client.post(f"/api/rounds/{round_row.id}/request-ai")

    page = client.get(f"/ui/rounds/{round_row.id}")
    assert "Решения скрыты" in page.text

    for decision in rounds_service.decisions_for(db, round_row.id):
        reason = (decision.payload or {}).get("short_reason")
        if reason:
            assert reason not in page.text


def test_round_locks_only_after_all_three(db: Session, seeded, market: Market, client):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    db.commit()
    client.post(f"/api/rounds/{round_row.id}/request-ai")

    db.expire_all()
    refreshed = db.get(type(round_row), round_row.id)
    assert refreshed.status == RoundStatus.OPEN.value

    client.post(
        f"/api/rounds/{round_row.id}/decisions/titan",
        json={
            "action": "HOLD",
            "estimated_probability": 0.5,
            "stake_usdc": 0,
            "confidence": 0.5,
            "short_reason": "пропускаю раунд",
        },
    )
    db.expire_all()
    refreshed = db.get(type(round_row), round_row.id)
    assert refreshed.status == RoundStatus.LOCKED.value
