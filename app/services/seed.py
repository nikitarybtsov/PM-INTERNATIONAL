"""Инициализация участников и демо-данных."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.constants import LedgerType, ParticipantKey, ParticipantKind
from app.db.models import Market, Participant, Portfolio
from app.services import audit
from app.services import portfolio as pf
from app.services.snapshots import provider_for, upsert_market

logger = logging.getLogger(__name__)

PARTICIPANT_SPECS = [
    (ParticipantKey.CODEX, "Codex", ParticipantKind.AI, "codex"),
    (ParticipantKey.CLAUDE, "Claude", ParticipantKind.AI, "claude"),
    (ParticipantKey.TITAN, "Titan (человек)", ParticipantKind.HUMAN, "manual"),
]


def seed_participants(db: Session) -> list[Participant]:
    """Создать трёх участников с независимыми банками по $1000."""
    settings = get_settings()
    created: list[Participant] = []
    for key, name, kind, adapter in PARTICIPANT_SPECS:
        participant = db.scalar(select(Participant).where(Participant.key == key.value))
        if participant is None:
            participant = Participant(
                key=key.value, display_name=name, kind=kind.value, adapter=adapter
            )
            db.add(participant)
            db.flush()
            portfolio = Portfolio(
                participant_id=participant.id,
                initial_balance=settings.initial_bankroll_usdc,
                cash_balance=settings.initial_bankroll_usdc,
                reserved_balance=0.0,
            )
            db.add(portfolio)
            db.flush()
            pf.add_ledger(
                db,
                participant_id=participant.id,
                entry_type=LedgerType.INITIAL,
                amount=settings.initial_bankroll_usdc,
                note="стартовый банк",
            )
            audit.record(
                db,
                entity_type="participant",
                entity_id=participant.id,
                action="create",
                after={"key": key.value, "initial_balance": settings.initial_bankroll_usdc},
            )
        created.append(participant)
    return created


def seed_markets(db: Session, query: str | None = None, limit: int = 10) -> list[Market]:
    """Загрузить рынки из активного провайдера (по умолчанию — демо-набор)."""
    settings = get_settings()
    provider = provider_for(db)
    refs = provider.search_markets(query or settings.polymarket_search_query, limit=limit)
    return [upsert_market(db, ref) for ref in refs]


def seed_all(db: Session, *, with_markets: bool = True) -> dict:
    participants = seed_participants(db)
    markets = seed_markets(db) if with_markets else []
    return {
        "participants": [p.key for p in participants],
        "markets": [{"id": m.id, "title": m.title} for m in markets],
    }
