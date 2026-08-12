"""Автоматическое ведение раундов.

Один тик планировщика делает три вещи:

  1. подтягивает рынки Dota 2 / The International у провайдера;
  2. для матчей, стартующих в ближайшее окно, открывает раунд:
     фиксирует snapshot, запрашивает Codex и Claude, шлёт Титану ссылку;
  3. следит за дедлайном ожидания Титана.

Титан — человек, автоматизировать его нельзя. Поэтому «автоматический режим»
означает: система сама доводит раунд до состояния «ждём человека» и сама
разбирается с тем, что делать, если человек не успел.

Политика на просрочку задаётся `TITAN_TIMEOUT_POLICY`:
  * `cancel` (по умолчанию) — раунд отменяется. Честно: за Титана никто
    решения не выдумывает, статистика прогнозов не портится;
  * `hold`   — за Титана записывается HOLD и раунд исполняется. Удобнее для
    непрерывности, но HOLD попадёт в его статистику как собственное решение.

Исполнение раунда остаётся бумажным: планировщик вызывает тот же
`rounds.execute_round`, что и кнопка оператора.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data import MarketNotAvailable
from app.config import get_settings
from app.constants import MarketStatus, Phase, RoundStatus
from app.db.models import Market, Participant, Round, Settlement
from app.schemas.decision import TradeDecisionInput
from app.services import notifications, settlement
from app.services import rounds as rounds_service
from app.services import seed as seed_service

logger = logging.getLogger(__name__)

TITAN_TIMEOUT_HOLD = TradeDecisionInput(
    action="HOLD",
    estimated_probability=0.5,
    stake_usdc=0,
    max_acceptable_price=None,
    confidence=0.5,
    short_reason="Автоматический HOLD: решение не подано до дедлайна.",
    key_factors=[],
    risk_factors=["решение не принято человеком"],
    information_used=["политика TITAN_TIMEOUT_POLICY=hold"],
)


@dataclass
class TickResult:
    """Что произошло за один проход планировщика."""

    markets_refreshed: int = 0
    rounds_opened: list[int] = field(default_factory=list)
    rounds_executed: list[int] = field(default_factory=list)
    rounds_cancelled: list[int] = field(default_factory=list)
    reminders_sent: list[int] = field(default_factory=list)
    markets_settled: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "markets_refreshed": self.markets_refreshed,
            "rounds_opened": self.rounds_opened,
            "rounds_executed": self.rounds_executed,
            "rounds_cancelled": self.rounds_cancelled,
            "markets_settled": self.markets_settled,
            "reminders_sent": self.reminders_sent,
            "errors": self.errors,
        }

    @property
    def did_anything(self) -> bool:
        return bool(
            self.rounds_opened
            or self.rounds_executed
            or self.rounds_cancelled
            or self.reminders_sent
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _open_rounds(db: Session) -> list[Round]:
    return list(
        db.scalars(
            select(Round).where(
                Round.status.in_([RoundStatus.OPEN.value, RoundStatus.LOCKED.value])
            )
        )
    )


def _is_settled(db: Session, market_id: int) -> bool:
    return db.scalar(select(Settlement).where(Settlement.market_id == market_id)) is not None


def _deadline_for(db: Session, round_row: Round) -> datetime:
    """Дедлайн ожидания Титана: раньшее из «за N минут до матча» и «TTL снапшота»."""
    settings = get_settings()
    created = _aware(round_row.created_at) or datetime.now(UTC)
    by_policy = created + timedelta(minutes=settings.titan_deadline_minutes)

    market = db.get(Market, round_row.market_id)
    starts_at = _aware(market.starts_at) if market else None
    if starts_at:
        by_match = starts_at - timedelta(minutes=settings.scheduler_min_before_match_minutes)
        return min(by_policy, by_match)
    return by_policy


# ---------------------------------------------------------------------------
def refresh_markets(db: Session) -> int:
    settings = get_settings()
    try:
        markets = seed_service.seed_markets(db, query=settings.scheduler_market_query, limit=25)
    except MarketNotAvailable as exc:
        logger.warning("не удалось обновить рынки: %s", exc)
        return 0
    return len(markets)


def candidate_markets(db: Session, now: datetime | None = None) -> list[Market]:
    """Матчи, для которых пора открыть прематч-раунд."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    window_start = now + timedelta(minutes=settings.scheduler_min_before_match_minutes)
    window_end = now + timedelta(minutes=settings.scheduler_open_before_match_minutes)

    busy_market_ids = {r.market_id for r in _open_rounds(db)}
    result: list[Market] = []

    for market in db.scalars(select(Market).order_by(Market.starts_at)):
        if market.status != MarketStatus.OPEN.value:
            continue
        if market.id in busy_market_ids or _is_settled(db, market.id):
            continue
        starts_at = _aware(market.starts_at)
        if starts_at is None:
            continue
        if not (window_start <= starts_at <= window_end):
            continue
        # раунд по этому рынку уже проводился — второй прематч не нужен
        already = db.scalar(
            select(Round).where(
                Round.market_id == market.id,
                Round.phase == Phase.PREMATCH.value,
                Round.status.in_(
                    [RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value]
                ),
            )
        )
        if already is not None:
            continue
        result.append(market)
    return result


def open_round(db: Session, market: Market) -> Round | None:
    """Открыть раунд и сразу собрать решения ИИ."""
    try:
        round_row = rounds_service.create_round(
            db, market, Phase.PREMATCH, note="открыт планировщиком", actor="scheduler"
        )
    except (rounds_service.RoundStateError, MarketNotAvailable, ValueError) as exc:
        logger.warning("не удалось открыть раунд по %s: %s", market.external_id, exc)
        return None

    notifications.round_opened(db, round_row)

    stored = rounds_service.request_ai_decisions(db, round_row)
    statuses = {}
    for decision in stored:
        participant = db.get(Participant, decision.participant_id)
        if participant:
            statuses[participant.key] = decision.status
    if statuses:
        notifications.ai_collected(db, round_row, statuses)

    return round_row


def resolve_pending(db: Session, round_row: Round, now: datetime | None = None) -> str | None:
    """Довести раунд до конца, если пора. Возвращает совершённое действие."""
    settings = get_settings()
    now = now or datetime.now(UTC)

    if round_row.status == RoundStatus.LOCKED.value:
        report = rounds_service.execute_round(db, round_row, actor="scheduler")
        rounds_service.reveal_round(db, round_row, actor="scheduler")
        notifications.round_executed(db, round_row, report)
        return "executed"

    if round_row.status != RoundStatus.OPEN.value:
        return None

    deadline = _deadline_for(db, round_row)
    if now < deadline:
        minutes_left = int((deadline - now).total_seconds() // 60)
        if minutes_left <= 5:
            notifications.titan_reminder(round_row, max(minutes_left, 0))
            return "reminded"
        return None

    # дедлайн прошёл
    if settings.titan_timeout_policy == "hold":
        titan = rounds_service.get_participant(db, "titan")
        if rounds_service.existing_decision(db, round_row.id, titan.id) is None:
            rounds_service.submit_manual_decision(
                db, round_row, "titan", TITAN_TIMEOUT_HOLD, actor="scheduler"
            )
        report = rounds_service.execute_round(db, round_row, actor="scheduler")
        rounds_service.reveal_round(db, round_row, actor="scheduler")
        notifications.round_executed(db, round_row, report)
        return "executed"

    reason = "Титан не подал решение до дедлайна"
    rounds_service.cancel_round(db, round_row, reason, actor="scheduler")
    notifications.round_cancelled(round_row, reason)
    return "cancelled"


def tick(db: Session, now: datetime | None = None) -> TickResult:
    """Один проход планировщика. Идемпотентен и безопасен при повторе."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    result = TickResult()

    # Расчёт рынков работает независимо от планировщика раундов: даже если
    # раунды открываются руками, результаты должны приезжать сами.
    if settings.auto_settle_enabled:
        try:
            report = settlement.auto_settle(db)
            for item in report["settled"]:
                market = db.get(Market, item["market_id"])
                if market is not None:
                    notifications.market_settled(db, market, item["outcome"])
                result.markets_settled.append(item["market_id"])
        except Exception as exc:  # noqa: BLE001 — тик не должен падать целиком
            logger.exception("сбой автоматического расчёта рынков")
            result.errors.append(f"settle: {exc}")

    if not settings.scheduler_enabled:
        return result

    try:
        result.markets_refreshed = refresh_markets(db)
    except Exception as exc:  # noqa: BLE001 — тик не должен падать целиком
        logger.exception("сбой обновления рынков")
        result.errors.append(f"refresh: {exc}")

    # сначала закрываем то, что уже висит
    for round_row in _open_rounds(db):
        try:
            action = resolve_pending(db, round_row, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.exception("сбой обработки раунда #%s", round_row.id)
            result.errors.append(f"round {round_row.id}: {exc}")
            continue
        if action == "executed":
            result.rounds_executed.append(round_row.id)
        elif action == "cancelled":
            result.rounds_cancelled.append(round_row.id)
        elif action == "reminded":
            result.reminders_sent.append(round_row.id)

    # затем открываем новые, соблюдая потолок одновременных раундов
    free_slots = settings.scheduler_max_open_rounds - len(_open_rounds(db))
    if free_slots > 0:
        for market in candidate_markets(db, now=now)[:free_slots]:
            try:
                round_row = open_round(db, market)
            except Exception as exc:  # noqa: BLE001
                logger.exception("сбой открытия раунда по %s", market.external_id)
                result.errors.append(f"open {market.external_id}: {exc}")
                continue
            if round_row is not None:
                result.rounds_opened.append(round_row.id)

    return result
