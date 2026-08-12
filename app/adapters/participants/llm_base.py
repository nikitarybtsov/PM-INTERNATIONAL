"""Общая механика LLM-участников: ретраи, таймауты, валидация JSON.

Codex и Claude отличаются только транспортом (`_call_model`). Логика
валидации, число попыток и обработка ошибок у них полностью одинаковы.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod

from app.adapters.participants.base import (
    ParticipantAdapter,
    ParticipantError,
    ParticipantResult,
    PortfolioView,
)
from app.adapters.participants.mock_brain import mock_decision
from app.adapters.participants.prompting import build_prompt, prompt_version
from app.config import get_settings
from app.schemas.decision import TradeDecisionInput, parse_decision_json
from app.schemas.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

_RETRY_HINT = (
    "\n\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ ОТКЛОНЁН: {error}\n"
    "Верни ровно один валидный JSON-объект по схеме, без текста вокруг."
)


class LLMParticipantAdapter(ParticipantAdapter):
    kind = "AI"
    provider: str = "llm"

    def __init__(self, *, mock: bool | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._mock = self._resolve_mock(mock)
        self.timeout = settings.participant_timeout_seconds
        self.max_retries = max(0, settings.participant_max_retries)

    # ---- переопределяется в наследниках ------------------------------------
    def _resolve_mock(self, mock: bool | None) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    def _call_model(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        """Вернуть (текст ответа, версия модели). Может бросить исключение."""

    # ---- общий цикл --------------------------------------------------------
    @property
    def is_mock(self) -> bool:
        return self._mock

    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> ParticipantResult:
        started = time.perf_counter()
        if self._mock:
            decision = mock_decision(self.key, snapshot, portfolio)
            return ParticipantResult(
                decision=decision,
                model_name=f"mock:{self.key}",
                model_version="mock-1",
                prompt_version=prompt_version(),
                attempts=1,
                latency_ms=int((time.perf_counter() - started) * 1000),
                raw_response=decision.model_dump_json(),
            )

        system_prompt, user_prompt = build_prompt(snapshot, portfolio)
        errors: list[dict] = []
        last_raw: str | None = None
        attempt = 0

        while attempt <= self.max_retries:
            attempt += 1
            prompt = user_prompt
            if errors:
                prompt = user_prompt + _RETRY_HINT.format(error=errors[-1]["message"][:300])
            try:
                raw, model_version = self._call_model(system_prompt, prompt)
                last_raw = raw
                decision: TradeDecisionInput = parse_decision_json(raw)
            except Exception as exc:  # noqa: BLE001 — фиксируем любую ошибку провайдера
                errors.append(
                    {
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:1000],
                    }
                )
                logger.warning(
                    "участник %s: попытка %s неуспешна (%s)", self.key, attempt, type(exc).__name__
                )
                continue

            return ParticipantResult(
                decision=decision,
                model_name=self.model_name,
                model_version=model_version,
                prompt_version=prompt_version(),
                attempts=attempt,
                latency_ms=int((time.perf_counter() - started) * 1000),
                raw_response=last_raw,
                errors=errors,
            )

        raise ParticipantError(
            f"{self.key}: не удалось получить валидное решение за {attempt} попыток; "
            f"последняя ошибка: {errors[-1]['message'] if errors else 'неизвестна'}",
            error_type=errors[-1]["error_type"] if errors else "unknown",
            attempts=attempt,
        )
