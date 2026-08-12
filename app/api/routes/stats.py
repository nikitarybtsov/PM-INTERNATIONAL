"""Статистика и таблица результатов."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import AuditEvent, Participant
from app.services import stats as stats_service

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/scoreboard")
def scoreboard(db: Session = Depends(get_db)) -> dict:
    return stats_service.scoreboard(db)


@router.get("/participants/{participant_key}")
def participant(participant_key: str, db: Session = Depends(get_db)) -> dict:
    row = db.scalar(select(Participant).where(Participant.key == participant_key))
    if row is None:
        raise HTTPException(status_code=404, detail="участник не найден")
    return stats_service.participant_stats(db, row).to_dict()


@router.get("/biggest-moves")
def biggest_moves(limit: int = 5, db: Session = Depends(get_db)) -> dict:
    return stats_service.biggest_moves(db, limit=limit)


@router.get("/disagreements")
def disagreements(limit: int = 10, db: Session = Depends(get_db)) -> list[dict]:
    return stats_service.disagreements(db, limit=limit)


@router.get("/audit")
def audit_log(limit: int = 200, db: Session = Depends(get_db)) -> list[dict]:
    """Журнал аудита — доказательство, что решения не менялись задним числом."""
    return [
        {
            "id": e.id,
            "entity_type": e.entity_type,
            "entity_id": e.entity_id,
            "action": e.action,
            "actor": e.actor,
            "before": e.before,
            "after": e.after,
            "note": e.note,
            "created_at": e.created_at,
        }
        for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))
    ]
