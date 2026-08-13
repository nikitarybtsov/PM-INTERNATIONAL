"""ORM-модели. Хранится полный аудиторский след эксперимента.

Принцип неизменяемости: решения, snapshot'ы, фиксы и ордера не редактируются
задним числом. Любая правка проходит через `services.audit.record` и создаёт
новую запись `audit_events` с before/after.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import (
    DecisionStatus,
    MarketStatus,
    OrderStatus,
    PositionStatus,
    RoundStatus,
)
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Участники и банки
# ---------------------------------------------------------------------------
class Participant(Base, TimestampMixin):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # AI | HUMAN
    adapter: Mapped[str] = mapped_column(String(32))  # codex | claude | manual

    portfolio: Mapped[Portfolio] = relationship(back_populates="participant", uselist=False)


class Portfolio(Base, TimestampMixin):
    """Отдельный независимый банк участника."""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), unique=True
    )
    initial_balance: Mapped[float] = mapped_column(Float, default=1000.0)
    cash_balance: Mapped[float] = mapped_column(Float, default=1000.0)
    reserved_balance: Mapped[float] = mapped_column(Float, default=0.0)

    participant: Mapped[Participant] = relationship(back_populates="portfolio")

    @property
    def available_balance(self) -> float:
        return round(self.cash_balance - self.reserved_balance, 2)


class LedgerEntry(Base, TimestampMixin):
    """Каждое движение денег. Основа для кривой банка и просадки."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(24))
    amount: Mapped[float] = mapped_column(Float)  # знаковая величина
    cash_after: Mapped[float] = mapped_column(Float)
    equity_after: Mapped[float] = mapped_column(Float)
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Рынки и snapshot'ы
# ---------------------------------------------------------------------------
class Market(Base, TimestampMixin):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="mock")
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    event_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tournament: Mapped[str | None] = mapped_column(String(128), nullable=True)
    market_type: Mapped[str] = mapped_column(String(64), default="MATCH_WINNER")
    team_a: Mapped[str | None] = mapped_column(String(128), nullable=True)
    team_b: Mapped[str | None] = mapped_column(String(128), nullable=True)
    yes_label: Mapped[str] = mapped_column(String(128), default="YES")
    no_label: Mapped[str] = mapped_column(String(128), default="NO")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=MarketStatus.OPEN.value)
    operator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Токены исходов на CLOB — то, что реально покупается на бирже
    yes_token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    no_token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_market_source_ext"),)


class RawMarketPayload(Base, TimestampMixin):
    """Сырые ответы источника — сохраняются как есть для аудита."""

    __tablename__ = "raw_market_payloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSON)


class Snapshot(Base, TimestampMixin):
    """Immutable-снимок рынка. Все участники видят строго один и тот же payload."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    phase: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=900)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id"), nullable=True
    )
    map_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    market: Mapped[Market] = relationship()


# ---------------------------------------------------------------------------
# Раунды и решения
# ---------------------------------------------------------------------------
class Round(Base, TimestampMixin):
    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    phase: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=RoundStatus.OPEN.value, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    market: Mapped[Market] = relationship()
    snapshot: Mapped[Snapshot] = relationship()
    decisions: Mapped[list[Decision]] = relationship(back_populates="round")

    # Один живой раунд на рынок — на уровне БД. Проверка в коде не спасает от
    # гонки: два одновременных запроса читают базу до того, как первый успеет
    # записаться, и оба видят «свободно». Так появились раунды #9 и #10 с
    # разницей в 1.8 секунды, после чего решения первого сгорели как устаревшие.
    __table_args__ = (
        Index(
            "ux_active_round_per_market",
            "market_id",
            unique=True,
            sqlite_where=text(
                "status IN ('OPEN', 'LOCKED', 'AWAITING_APPROVAL')"
            ),
            postgresql_where=text(
                "status IN ('OPEN', 'LOCKED', 'AWAITING_APPROVAL')"
            ),
        ),
    )


class Decision(Base, TimestampMixin):
    """Решение участника. Одно на раунд, редактированию не подлежит."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("round_id", "participant_id", name="uq_decision_round_participant"),
        Index("ix_decision_round", "round_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"))

    status: Mapped[str] = mapped_column(String(16), default=DecisionStatus.VALID.value)
    locked: Mapped[bool] = mapped_column(Boolean, default=True)

    # содержимое TradeDecision (валидированное) либо сырой ответ при INVALID
    payload: Mapped[dict] = mapped_column(JSON)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    estimated_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    # edge после комиссии тейкера — по нему решается, стоит ли входить
    net_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_fee_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    stake_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_acceptable_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Одобрение оператора. В боевом режиме без него сделка не исполняется —
    # это третий предохранитель, см. app/config.py.
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Реальный ордер на бирже, если сделка исполнялась вживую
    live_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)

    round: Mapped[Round] = relationship(back_populates="decisions")
    participant: Mapped[Participant] = relationship()


class RiskEvaluation(Base, TimestampMixin):
    """Решение ДО и ПОСЛЕ risk engine + журнал причин."""

    __tablename__ = "risk_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), unique=True)
    verdict: Mapped[str] = mapped_column(String(16))
    decision_before: Mapped[dict] = mapped_column(JSON)
    decision_after: Mapped[dict] = mapped_column(JSON)
    requested_stake: Mapped[float] = mapped_column(Float, default=0.0)
    approved_stake: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    limits_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Исполнение (только симуляция)
# ---------------------------------------------------------------------------
class SimulatedOrder(Base, TimestampMixin):
    __tablename__ = "simulated_orders"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_order_decision"),  # защита от дублей
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"))
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"))

    action: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(8))  # YES | NO
    limit_price: Mapped[float] = mapped_column(Float)
    requested_notional: Mapped[float] = mapped_column(Float, default=0.0)
    requested_size: Mapped[float] = mapped_column(Float, default=0.0)
    filled_size: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default=OrderStatus.PENDING.value)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[str | None] = mapped_column(String(64), nullable=True)

    fills: Mapped[list[Fill]] = relationship(back_populates="order")


class Fill(Base, TimestampMixin):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("simulated_orders.id", ondelete="CASCADE"))
    level_index: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)

    order: Mapped[SimulatedOrder] = relationship(back_populates="fills")


class Position(Base, TimestampMixin):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("participant_id", "market_id", "outcome", name="uq_position_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(8))
    size: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default=PositionStatus.OPEN.value)
    opened_round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phase_opened: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Settlement(Base, TimestampMixin):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), unique=True)
    winning_outcome: Mapped[str] = mapped_column(String(8))  # YES | NO
    source: Mapped[str] = mapped_column(String(32), default="manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Аудит и ошибки
# ---------------------------------------------------------------------------
class ApiErrorLog(Base, TimestampMixin):
    __tablename__ = "api_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"), nullable=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AuditEvent(Base, TimestampMixin):
    """Неизменяемый журнал. Правки создают новые записи, а не перезаписывают старые."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(48), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
