"""DTO входящих и исходящих HTTP-запросов."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.constants import Action, Phase


class CreateRoundRequest(BaseModel):
    market_id: int
    phase: Phase = Phase.PREMATCH
    operator_context: str | None = Field(
        default=None, description="Составы, замены, новости турнира — видно всем участникам"
    )
    map_number: int | None = Field(
        default=None, description="Обязателен для BETWEEN_MAPS: номер завершённой карты"
    )
    note: str | None = None


class ManualDecisionRequest(BaseModel):
    action: Action
    estimated_probability: float = Field(ge=0.0, le=1.0)
    stake_usdc: float = Field(ge=0.0, default=0.0)
    max_acceptable_price: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    short_reason: str
    key_factors: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    information_used: list[str] = Field(default_factory=list)


class SettleRequest(BaseModel):
    winning_outcome: str = Field(pattern="^(YES|NO)$")
    note: str | None = None


class SeedRequest(BaseModel):
    with_markets: bool = True
    query: str | None = None
    limit: int = 10


class OperatorNotesRequest(BaseModel):
    notes: str


class ConfirmMapRequest(BaseModel):
    """Оператор подтверждает окончание карты и просит новый snapshot."""

    map_number: int = Field(ge=1, description="Номер только что завершившейся карты")
    operator_context: str | None = None
    note: str | None = None
