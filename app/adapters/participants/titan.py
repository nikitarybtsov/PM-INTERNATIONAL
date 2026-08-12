"""Участник Titan — человек. Решение приходит из веб-формы, а не из API.

Адаптер существует ради единообразия интерфейса: он не может «сходить»
за решением сам и всегда сообщает, что раунд ждёт ручного ввода.
"""

from __future__ import annotations

import time

from app.adapters.participants.base import (
    ParticipantAdapter,
    ParticipantError,
    ParticipantResult,
    PortfolioView,
)
from app.adapters.participants.prompting import prompt_version
from app.constants import ParticipantKey
from app.schemas.decision import TradeDecisionInput
from app.schemas.snapshot import MarketSnapshot

TITAN_MODEL_NAME = "human:titan"
TITAN_MODEL_VERSION = "dota2-titan-rank"


class TitanManualAdapter(ParticipantAdapter):
    key = ParticipantKey.TITAN.value
    kind = "HUMAN"

    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> ParticipantResult:
        raise ParticipantError(
            "Titan вводит решение вручную через интерфейс /ui/rounds/{id}/titan",
            error_type="manual_input_required",
        )

    @staticmethod
    def wrap_manual(decision: TradeDecisionInput, latency_ms: int | None = None) -> ParticipantResult:
        """Обернуть решение, введённое человеком, в стандартный результат."""
        return ParticipantResult(
            decision=decision,
            model_name=TITAN_MODEL_NAME,
            model_version=TITAN_MODEL_VERSION,
            prompt_version=prompt_version(),
            attempts=1,
            latency_ms=latency_ms if latency_ms is not None else int(time.time() * 0),
            raw_response=decision.model_dump_json(),
        )
