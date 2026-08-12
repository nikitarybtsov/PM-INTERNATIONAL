"""Формирование текстов уведомлений по этапам раунда.

ГРАНИЦА ПРИВАТНОСТИ проходит здесь. Функции разделены на две группы:

  * `round_opened`, `ai_collected`, `titan_reminder` — до раскрытия.
    Несут только факты: какой рынок, кто уже ответил, сколько осталось.
    Содержимое решений в них не попадает НИКОГДА.

  * `round_executed`, `market_settled`, `daily_digest` — после исполнения.
    Раунд уже в статусе EXECUTED/REVEALED, решения раскрыты официально.

Тест `tests/test_notifications.py` проверяет, что обоснования участников
не встречаются в сообщениях первой группы.
"""

from __future__ import annotations

import html
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.notifiers import get_notifier
from app.config import get_settings
from app.constants import RoundStatus
from app.db.models import Market, Participant, RiskEvaluation, Round, SimulatedOrder, Snapshot
from app.services import rounds as rounds_service
from app.services import stats as stats_service

logger = logging.getLogger(__name__)


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else "—"))


def _titan_link(round_id: int) -> str:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    token = settings.titan_access_token
    suffix = f"?t={token}" if token else ""
    return f"{base}/ui/rounds/{round_id}/titan{suffix}"


def _operator_link(round_id: int) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/ui/rounds/{round_id}"


def _send(text: str) -> bool:
    try:
        return get_notifier().send(text)
    except Exception:  # noqa: BLE001 — уведомление не имеет права ломать раунд
        logger.exception("сбой отправки уведомления")
        return False


# ---------------------------------------------------------------------------
# ДО РАСКРЫТИЯ — только факты, без содержимого решений
# ---------------------------------------------------------------------------
def round_opened(db: Session, round_row: Round) -> bool:
    market = db.get(Market, round_row.market_id)
    snap = db.get(Snapshot, round_row.snapshot_id)
    payload = snap.payload if snap else {}

    yes_label = _esc((payload.get("market") or {}).get("yes_label"))
    no_label = _esc((payload.get("market") or {}).get("no_label"))
    starts_at = (payload.get("market") or {}).get("starts_at") or "—"

    text = (
        f"🎯 <b>Новый раунд #{round_row.id}</b>\n"
        f"{_esc(market.title if market else '')}\n\n"
        f"Фаза: <b>{_esc(round_row.phase)}</b>\n"
        f"Начало матча: {_esc(starts_at)}\n"
        f"Цена: {yes_label} <b>{payload.get('yes_price')}</b> · "
        f"{no_label} <b>{payload.get('no_price')}</b>\n"
        f"Ликвидность: {payload.get('liquidity_usdc')} USDC\n"
        f"Snapshot #{round_row.snapshot_id} · hash <code>{_esc((snap.payload_hash if snap else '')[:12])}</code>\n\n"
        f"⏳ Ждём решение Титана: {_titan_link(round_row.id)}"
    )
    return _send(text)


def ai_collected(db: Session, round_row: Round, statuses: dict[str, str]) -> bool:
    """Факт получения решений ИИ. Содержимое не раскрывается."""
    lines = []
    for key, status in sorted(statuses.items()):
        icon = "✅" if status == "VALID" else "⚠️"
        lines.append(f"{icon} {_esc(key)} — {_esc(status)}")

    state = rounds_service.public_round_state(db, round_row)
    text = (
        f"🤖 <b>Раунд #{round_row.id}: ИИ ответили</b>\n"
        + "\n".join(lines)
        + f"\n\nЖдём: <b>{_esc(', '.join(state['awaiting']) or '—')}</b>\n"
        f"<i>Содержимое решений скрыто до исполнения раунда.</i>"
    )
    return _send(text)


def titan_reminder(round_row: Round, minutes_left: int) -> bool:
    text = (
        f"⏰ <b>Раунд #{round_row.id}</b>: до дедлайна {minutes_left} мин.\n"
        f"Решение Титана ещё не подано.\n{_titan_link(round_row.id)}"
    )
    return _send(text)


def round_cancelled(round_row: Round, reason: str) -> bool:
    text = f"🚫 <b>Раунд #{round_row.id} отменён</b>\nПричина: {_esc(reason)}"
    return _send(text)


# ---------------------------------------------------------------------------
# ПОСЛЕ ИСПОЛНЕНИЯ — решения уже раскрыты официально
# ---------------------------------------------------------------------------
def round_executed(db: Session, round_row: Round, report: dict) -> bool:
    if round_row.status not in (RoundStatus.EXECUTED.value, RoundStatus.REVEALED.value):
        logger.error(
            "попытка отправить раскрытие для нераскрытого раунда #%s — заблокировано",
            round_row.id,
        )
        return False

    market = db.get(Market, round_row.market_id)
    blocks = [f"📊 <b>Раунд #{round_row.id} исполнен</b>\n{_esc(market.title if market else '')}\n"]

    for decision in rounds_service.decisions_for(db, round_row.id):
        participant = db.get(Participant, decision.participant_id)
        key = participant.key if participant else "?"
        entry = report.get(key, {})
        payload = decision.payload or {}
        evaluation = db.scalar(
            select(RiskEvaluation).where(RiskEvaluation.decision_id == decision.id)
        )
        order = db.scalar(select(SimulatedOrder).where(SimulatedOrder.decision_id == decision.id))

        lines = [f"\n<b>{_esc(key.upper())}</b> — {_esc(payload.get('action', decision.status))}"]
        if decision.estimated_probability is not None:
            lines.append(
                f"P(YES) {decision.estimated_probability:.3f} против рынка "
                f"{(decision.market_probability or 0):.3f} · edge "
                f"{(decision.edge or 0):+.3f}"
            )
        if evaluation:
            lines.append(
                f"Risk: {_esc(evaluation.verdict)} "
                f"{evaluation.requested_stake:.2f} → {evaluation.approved_stake:.2f} USDC"
            )
            for reason in evaluation.reasons or []:
                if reason.get("severity") in ("adjust", "reject"):
                    lines.append(f"  • {_esc(reason.get('message'))}")
        if order and order.filled_size:
            lines.append(
                f"Исполнено: {order.outcome} {order.filled_size:.2f} шт @ "
                f"{order.avg_fill_price:.4f} ({order.notional:.2f} USDC)"
            )
        elif entry.get("executed") is False:
            lines.append("Сделки не было")
        reason_text = payload.get("short_reason")
        if reason_text:
            lines.append(f"<i>{_esc(reason_text)}</i>")
        blocks.append("\n".join(lines))

    blocks.append(f"\n\n{_operator_link(round_row.id)}")
    return _send("".join(blocks))


def market_settled(db: Session, market: Market, winning_outcome: str) -> bool:
    board = stats_service.scoreboard(db)
    label = market.yes_label if winning_outcome == "YES" else market.no_label
    lines = [
        f"🏁 <b>Результат: {_esc(market.title)}</b>",
        f"Победил <b>{_esc(label)}</b> ({winning_outcome})\n",
        "<b>Банки:</b>",
    ]
    for p in board["participants"]:
        sign = "🟢" if p["pnl_abs"] > 0 else ("🔴" if p["pnl_abs"] < 0 else "⚪")
        lines.append(
            f"{sign} {_esc(p['participant'])}: {p['equity']:.2f} $ "
            f"({p['pnl_abs']:+.2f} / {p['pnl_pct']:+.2f}%)"
        )
    return _send("\n".join(lines))


def daily_digest(db: Session) -> bool:
    board = stats_service.scoreboard(db)
    winners = board["winners"]
    lines = ["📅 <b>Дневной итог</b>\n", "<b>Скоборд:</b>"]
    for p in board["participants"]:
        brier = p["forecast"]["brier"]
        lines.append(
            f"• {_esc(p['participant'])}: {p['equity']:.2f} $ "
            f"({p['pnl_pct']:+.2f}%) · ставок {p['bets_count']} · "
            f"Brier {brier if brier is not None else '—'}"
        )
    lines.append(
        f"\n🏆 Лучший трейдер: <b>{_esc(winners['best_trader']['participant'])}</b>"
    )
    lines.append(
        f"🎯 Лучший прогнозист: <b>{_esc(winners['best_forecaster']['participant'])}</b>"
    )
    return _send("\n".join(lines))


def startup(db: Session) -> bool:
    settings = get_settings()
    text = (
        "🚀 <b>PM-INTERNATIONAL запущен</b>\n"
        f"Режим: <b>PAPER TRADING</b> (реальных сделок нет)\n"
        f"Источник рынков: {_esc(settings.market_data_provider)}\n"
        f"Codex: {_esc(settings.codex_transport)} · "
        f"Claude: {'API' if settings.has_anthropic() else 'mock'}\n"
        f"Планировщик: {'включён' if settings.scheduler_enabled else 'выключен'}\n"
        f"{_esc(settings.public_base_url)}"
    )
    return _send(text)
