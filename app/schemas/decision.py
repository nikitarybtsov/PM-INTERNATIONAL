"""Строгая схема TradeDecision.

Невалидный ответ участника НЕ МОЖЕТ быть исполнен: раунд сохраняет его со
статусом INVALID и передаёт в risk engine как автоматический REJECT.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants import Action

MAX_REASON_LEN = 600


class TradeDecisionInput(BaseModel):
    """То, что возвращает модель или вводит человек.

    Служебные поля (participant_id, snapshot_id, market_id, created_at,
    model_*, prompt_version) проставляет система — участник их не подделывает.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Action
    estimated_probability: float = Field(ge=0.0, le=1.0)
    stake_usdc: float = Field(ge=0.0)
    max_acceptable_price: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    short_reason: str = Field(min_length=3, max_length=MAX_REASON_LEN)
    key_factors: list[str] = Field(default_factory=list, max_length=10)
    risk_factors: list[str] = Field(default_factory=list, max_length=10)
    information_used: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("key_factors", "risk_factors", "information_used", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("\n", ";").split(";")]
            return [p for p in parts if p]
        return v

    @field_validator("key_factors", "risk_factors", "information_used")
    @classmethod
    def _trim_items(cls, v: list[str]) -> list[str]:
        return [item[:200] for item in v if item and item.strip()]

    @model_validator(mode="after")
    def _check_consistency(self) -> TradeDecisionInput:
        if self.action == Action.HOLD:
            if self.stake_usdc not in (0, 0.0):
                raise ValueError("HOLD не может иметь stake_usdc > 0")
        else:
            if self.stake_usdc <= 0:
                raise ValueError(f"{self.action.value} требует stake_usdc > 0")
            if self.action in (Action.BUY_YES, Action.BUY_NO):
                if self.max_acceptable_price is None:
                    raise ValueError("для покупки обязателен max_acceptable_price")
                if not 0 < self.max_acceptable_price <= 1:
                    raise ValueError("max_acceptable_price должен быть в (0, 1]")
        return self

    @property
    def outcome(self) -> str | None:
        """Исход, к которому относится действие."""
        if self.action == Action.BUY_YES:
            return "YES"
        if self.action == Action.BUY_NO:
            return "NO"
        return None


class TradeDecision(TradeDecisionInput):
    """Полное решение с системными полями. Именно оно сохраняется и исполняется."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    participant_id: str
    snapshot_id: int
    market_id: int
    market_probability: float = Field(ge=0.0, le=1.0)
    edge: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_name: str
    model_version: str
    prompt_version: str

    @classmethod
    def build(
        cls,
        *,
        decision: TradeDecisionInput,
        participant_id: str,
        snapshot_id: int,
        market_id: int,
        market_probability: float,
        model_name: str,
        model_version: str,
        prompt_version: str,
    ) -> TradeDecision:
        """Собрать полное решение, посчитав edge относительно рыночной вероятности.

        `estimated_probability` — всегда вероятность исхода YES (так сформулирован
        промпт и так считается Brier score). `market_probability` — цена того
        исхода, который участник покупает. Поэтому для BUY_NO обе величины нужно
        привести к одному исходу: P(NO) = 1 - P(YES), иначе edge сравнивает
        вероятность YES с ценой NO и получается бессмысленное число.
        """
        outcome = decision.outcome
        probability_of_outcome = (
            1.0 - decision.estimated_probability
            if outcome == "NO"
            else decision.estimated_probability
        )
        edge = round(probability_of_outcome - market_probability, 6)
        return cls(
            **decision.model_dump(),
            participant_id=participant_id,
            snapshot_id=snapshot_id,
            market_id=market_id,
            market_probability=round(market_probability, 6),
            edge=edge,
            model_name=model_name,
            model_version=model_version,
            prompt_version=prompt_version,
        )

    def payload(self) -> dict:
        return json.loads(self.model_dump_json())


def json_schema_for_prompt() -> str:
    """Схема, которую видят LLM-участники (без системных полей)."""
    schema = {
        "action": "BUY_YES | BUY_NO | SELL | HOLD",
        "estimated_probability": "число 0..1 — ваша оценка вероятности исхода YES",
        "stake_usdc": "число >= 0; 0 для HOLD",
        "max_acceptable_price": "число 0..1; обязательно для BUY_YES/BUY_NO",
        "confidence": "число 0..1",
        "short_reason": "строка до 600 символов",
        "key_factors": ["строка", "..."],
        "risk_factors": ["строка", "..."],
        "information_used": ["строка", "..."],
    }
    return json.dumps(schema, ensure_ascii=False, indent=2)


def parse_decision_json(raw: str) -> TradeDecisionInput:
    """Разобрать ответ модели. Терпимо к обёрткам ```json ... ```."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("в ответе нет JSON-объекта")
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON-объект")
    return TradeDecisionInput.model_validate(data)
