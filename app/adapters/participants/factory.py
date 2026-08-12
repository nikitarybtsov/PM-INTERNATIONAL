"""Реестр адаптеров участников."""

from __future__ import annotations

from app.adapters.participants.base import ParticipantAdapter
from app.adapters.participants.claude import ClaudeAdapter
from app.adapters.participants.codex import CodexAdapter
from app.adapters.participants.titan import TitanManualAdapter
from app.constants import ParticipantKey

_overrides: dict[str, ParticipantAdapter] = {}


def set_adapter_override(key: str, adapter: ParticipantAdapter | None) -> None:
    """Подмена адаптера в тестах и демо."""
    if adapter is None:
        _overrides.pop(key, None)
    else:
        _overrides[key] = adapter


def clear_adapter_overrides() -> None:
    _overrides.clear()


def get_participant_adapter(key: str) -> ParticipantAdapter:
    if key in _overrides:
        return _overrides[key]
    if key == ParticipantKey.CODEX.value:
        return CodexAdapter()
    if key == ParticipantKey.CLAUDE.value:
        return ClaudeAdapter()
    if key == ParticipantKey.TITAN.value:
        return TitanManualAdapter()
    raise ValueError(f"неизвестный участник: {key}")
