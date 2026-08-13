"""Слежение за драфтами и анализ по ходу пиков.

Что делает воркер на каждом тике:

1. смотрит турнирные матчи в эфире и сопоставляет их с рынками в базе;
2. как только у матча появились первые пики — шлёт уведомление «драфт начался»;
3. на срезах (по умолчанию 4, 8 и 10 выбранных героев) снимает snapshot и
   запрашивает обе модели. Промежуточные срезы дают видеть ход рассуждений,
   финальный — заявку, которую можно одобрить;
4. когда драфт закончен, шлёт заявки в Telegram со ссылкой на страницу.

Почему срезы, а не поток. `claude -p` и `codex exec` возвращают ответ целиком:
потока рассуждений наружу нет, «печатать мысли по мере появления» невозможно.
Срез — ближайшая честная замена: каждый виден на странице сразу, как приходит.

Почему не на каждый пик. Один проход обеих моделей занимает 1-3 минуты, а
драфт длится около пяти. Больше трёх срезов просто не поместится, и последний
не успеет к началу карты.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.constants import Phase, RoundStatus
from app.db.models import Market, Round
from app.services import draft as draft_service
from app.services import notifications
from app.services import rounds as rounds_service

logger = logging.getLogger(__name__)

# Состояние между тиками: сколько пиков уже отработано по каждому рынку.
_seen_picks: dict[int, int] = {}


@dataclass(slots=True)
class WatchResult:
    drafts_started: list[str] = field(default_factory=list)
    rounds_opened: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "drafts_started": self.drafts_started,
            "rounds_opened": self.rounds_opened,
            "errors": self.errors,
        }


def reset_state() -> None:
    """Сброс памяти о просмотренных пиках (тесты, рестарт)."""
    _seen_picks.clear()


def _checkpoints() -> list[int]:
    raw = get_settings().draft_checkpoints
    points = []
    for item in str(raw).split(","):
        item = item.strip()
        if item.isdigit():
            points.append(int(item))
    return sorted(points) or [10]


def _market_for(db: Session, live: draft_service.LiveDraft) -> Market | None:
    """Найти рынок серии по названиям команд из эфира."""
    wanted = {draft_service._norm(live.radiant_team), draft_service._norm(live.dire_team)}
    for market in db.scalars(select(Market)):
        if market.market_type != "MATCH_WINNER":
            continue
        pair = {draft_service._norm(market.team_a), draft_service._norm(market.team_b)}
        if pair == wanted:
            return market
    return None


def _context_for(live: draft_service.LiveDraft, market: Market) -> str:
    """Текст для snapshot: составы в том виде, в каком их видят обе модели."""
    stage = "драфт завершён" if live.is_complete else f"драфт идёт, выбрано {live.picks_total} из 10"
    # Radiant/Dire у Valve не совпадают с YES/NO рынка — раскладываем по названиям.
    a_is_radiant = draft_service._norm(market.team_a) == draft_service._norm(live.radiant_team)
    first = live.radiant_picks if a_is_radiant else live.dire_picks
    second = live.dire_picks if a_is_radiant else live.radiant_picks
    return (
        f"{stage}. Пики {market.team_a}: {', '.join(first) or '—'}. "
        f"Пики {market.team_b}: {', '.join(second) or '—'}. "
        f"Задержка трансляции {live.delay} с — цена могла сместиться."
    )


def tick(db: Session) -> WatchResult:
    """Один проход по матчам в эфире."""
    result = WatchResult()
    settings = get_settings()
    checkpoints = _checkpoints()

    for live in draft_service.fetch_all_live():
        if not live.picks_total:
            continue
        market = _market_for(db, live)
        if market is None:
            continue

        previous = _seen_picks.get(market.id, 0)
        if live.picks_total <= previous:
            continue  # ничего нового с прошлого тика

        # Первые пики — сообщаем, что драфт пошёл.
        if previous == 0:
            result.drafts_started.append(market.title)
            try:
                notifications.draft_started(db, market, live.picks_total)
            except Exception:  # noqa: BLE001 — уведомление не критично
                logger.exception("не удалось сообщить о начале драфта")

        _seen_picks[market.id] = live.picks_total

        # Срез пройден — снимаем snapshot и запрашиваем модели.
        if not any(previous < point <= live.picks_total for point in checkpoints):
            continue

        active = db.scalar(
            select(Round).where(
                Round.market_id == market.id,
                Round.status.in_(
                    [
                        RoundStatus.OPEN.value,
                        RoundStatus.LOCKED.value,
                        RoundStatus.AWAITING_APPROVAL.value,
                    ]
                ),
            )
        )
        if active is not None:
            # Раунд по этому рынку уже идёт: второй обесценил бы его решения.
            continue

        try:
            round_row = rounds_service.create_round(
                db,
                market,
                Phase.AFTER_DRAFT,
                map_number=None,
                operator_context=_context_for(live, market),
                actor="draft-watcher",
            )
            db.commit()
            result.rounds_opened.append(round_row.id)
        except Exception as exc:  # noqa: BLE001 — один матч не должен ронять цикл
            db.rollback()
            result.errors.append(f"{market.title[:40]}: {exc}")
            continue

        if settings.auto_request_ai_on_snapshot:
            try:
                rounds_service.request_ai_decisions(db, round_row)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                result.errors.append(f"модели по {market.title[:30]}: {exc}")

    return result
