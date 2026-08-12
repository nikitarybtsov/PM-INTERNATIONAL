"""Аудит: любое значимое действие фиксируется отдельной записью.

Задним числом изменить решение бесследно нельзя: сущности не перезаписываются
без записи в `audit_events` с полями before/after.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ApiErrorLog, AuditEvent

logger = logging.getLogger(__name__)

_SECRET_HINTS = ("api_key", "apikey", "authorization", "secret", "token", "password", "private")


def scrub(value: Any) -> Any:
    """Рекурсивно убрать всё, что похоже на секрет."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(hint in str(k).lower() for hint in _SECRET_HINTS):
                out[k] = "***redacted***"
            else:
                out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(scrub(value), default=str))


def record(
    db: Session,
    *,
    entity_type: str,
    action: str,
    entity_id: int | None = None,
    actor: str = "system",
    before: Any = None,
    after: Any = None,
    note: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        before=_jsonable(before),
        after=_jsonable(after),
        note=note,
    )
    db.add(event)
    db.flush()
    return event


def log_api_error(
    db: Session,
    *,
    provider: str,
    error_type: str,
    message: str,
    participant_id: int | None = None,
    round_id: int | None = None,
    attempt: int = 1,
    context: dict | None = None,
) -> ApiErrorLog:
    entry = ApiErrorLog(
        provider=provider,
        participant_id=participant_id,
        round_id=round_id,
        attempt=attempt,
        error_type=error_type[:64],
        message=message[:4000],
        context=_jsonable(context),
    )
    db.add(entry)
    db.flush()
    return entry
