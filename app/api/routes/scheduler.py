"""Управление автоматическим планировщиком раундов."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.services import notifications
from app.services import scheduler as scheduler_service

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    candidates = scheduler_service.candidate_markets(db)
    return {
        "enabled": settings.scheduler_enabled,
        "interval_seconds": settings.scheduler_interval_seconds,
        "open_before_match_minutes": settings.scheduler_open_before_match_minutes,
        "min_before_match_minutes": settings.scheduler_min_before_match_minutes,
        "max_open_rounds": settings.scheduler_max_open_rounds,
        "titan_timeout_policy": settings.titan_timeout_policy,
        "titan_deadline_minutes": settings.titan_deadline_minutes,
        "candidates": [
            {"market_id": m.id, "title": m.title, "starts_at": m.starts_at} for m in candidates
        ],
    }


@router.post("/tick")
def run_tick(db: Session = Depends(get_db)) -> dict:
    """Прогнать один тик вручную — удобно для отладки и первого запуска."""
    return scheduler_service.tick(db).as_dict()


@router.post("/digest")
def send_digest(db: Session = Depends(get_db)) -> dict:
    """Отправить дневной итог в Telegram."""
    return {"sent": notifications.daily_digest(db)}
