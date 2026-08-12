"""Тесты экспорта для монтажа и неизменяемости аудита."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.constants import Phase
from app.db.models import AuditEvent, Market
from app.schemas.decision import TradeDecisionInput
from app.services import audit, export, settlement
from app.services import rounds as rounds_service

TITAN_BUY = TradeDecisionInput(
    action="BUY_YES",
    estimated_probability=0.7,
    stake_usdc=60,
    max_acceptable_price=0.95,
    confidence=0.8,
    short_reason="фаворит недооценён",
    key_factors=["форма"],
    risk_factors=["замена"],
)


def played_round(db: Session, market: Market, settle: str | None = "YES"):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    rounds_service.execute_round(db, round_row)
    rounds_service.reveal_round(db, round_row)
    if settle:
        settlement.settle_market(db, market, settle)
    return round_row


# --- экспорт ----------------------------------------------------------------
def test_csv_export_has_row_per_participant(db: Session, seeded, market: Market):
    played_round(db, market)
    text = export.to_csv(export.rounds_table(db))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 3
    assert {r["participant"] for r in rows} == {"codex", "claude", "titan"}
    titan = next(r for r in rows if r["participant"] == "titan")
    assert titan["short_reason"] == "фаворит недооценён"
    assert titan["action"] == "BUY_YES"


def test_json_export_structure(db: Session, seeded, market: Market):
    played_round(db, market)
    payload = export.full_payload(db)
    assert payload["mode"] == "PAPER_TRADING_ONLY"
    for key in ("scoreboard", "rounds", "biggest_moves", "disagreements", "daily_scoreboard"):
        assert key in payload
    json.dumps(payload, default=str)  # сериализуемо


def test_html_report_contains_scoreboard_and_chart(db: Session, seeded, market: Market):
    played_round(db, market)
    html = export.html_report(db)
    assert "Scoreboard" in html
    assert "PAPER TRADING" in html
    assert "<svg" in html
    assert "Лучший трейдер" in html
    assert "Лучший прогнозист" in html
    assert "фаворит недооценён" in html


def test_chart_svg_is_self_contained(db: Session, seeded, market: Market):
    played_round(db, market)
    svg = export.equity_chart_svg(db)
    assert svg.startswith("<svg")
    assert "polyline" in svg
    assert "http://" not in svg.replace("http://www.w3.org/2000/svg", "")


def test_write_all_creates_files(db: Session, seeded, market: Market):
    played_round(db, market)
    files = export.write_all(db)
    for key in ("csv", "json", "html", "svg", "daily_csv"):
        path = Path(files[key])
        assert path.exists(), key
        assert path.stat().st_size > 0 or key == "daily_csv"


def test_disagreements_are_exported(db: Session, seeded, market: Market):
    played_round(db, market)
    payload = export.full_payload(db)
    assert payload["disagreements"]
    assert "probability_spread" in payload["disagreements"][0]


# --- аудит ------------------------------------------------------------------
def test_audit_trail_records_lifecycle(db: Session, seeded, market: Market):
    played_round(db, market)
    events = db.query(AuditEvent).all()
    actions = {e.action for e in events}
    assert "create" in actions
    assert "submit" in actions
    assert "lock" in actions
    assert "execute" in actions
    assert "reveal" in actions
    assert "settle" in actions
    assert any(e.action.startswith("verdict:") for e in events)


def test_audit_events_are_append_only(db: Session, seeded, market: Market):
    """Изменение сущности не переписывает историю, а добавляет новую запись."""
    played_round(db, market)
    before = db.query(AuditEvent).count()
    first_event = db.query(AuditEvent).order_by(AuditEvent.id).first()

    audit.record(
        db,
        entity_type="market",
        entity_id=market.id,
        action="manual_correction",
        before={"title": market.title},
        after={"title": "исправленное название"},
        note="ручная правка оператором",
    )
    db.flush()

    after = db.query(AuditEvent).count()
    assert after == before + 1
    # старая запись не изменилась
    db.refresh(first_event)
    assert first_event.action != "manual_correction"


def test_decisions_are_locked_after_submit(db: Session, seeded, market: Market):
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    decision = rounds_service.decisions_for(db, round_row.id)[0]
    assert decision.locked is True


def test_api_errors_are_logged(db: Session, seeded, market: Market, monkeypatch):
    """Падение адаптера сохраняется как FAILED-решение и запись в api_errors."""
    from app.adapters.participants.base import ParticipantAdapter, ParticipantError
    from app.adapters.participants.factory import set_adapter_override
    from app.db.models import ApiErrorLog

    class BrokenAdapter(ParticipantAdapter):
        key = "codex"

        def decide(self, snapshot, portfolio):
            raise ParticipantError("сервис недоступен", error_type="timeout", attempts=3)

    set_adapter_override("codex", BrokenAdapter())
    round_row = rounds_service.create_round(db, market, Phase.PREMATCH)
    rounds_service.request_ai_decisions(db, round_row)

    failed = [d for d in rounds_service.decisions_for(db, round_row.id) if d.status == "FAILED"]
    assert failed
    assert db.query(ApiErrorLog).count() >= 1

    # невалидное/непришедшее решение не исполняется
    rounds_service.submit_manual_decision(db, round_row, "titan", TITAN_BUY)
    report = rounds_service.execute_round(db, round_row)
    assert report["codex"]["verdict"] == "REJECTED"
    assert report["codex"]["executed"] is False
