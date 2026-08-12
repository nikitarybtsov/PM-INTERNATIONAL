"""Экспорт данных для YouTube/Telegram.

Ничего не публикуется автоматически — создаются только файлы на диске,
которые оператор сам выкладывает куда хочет.
"""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Decision, Market, Participant, Round, SimulatedOrder
from app.services import stats as stats_service

CHART_W, CHART_H = 880, 320
PALETTE = {"codex": "#2f7ed8", "claude": "#d97a2b", "titan": "#3f9e5a"}


def export_dir() -> Path:
    path = Path(get_settings().export_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
def rounds_table(db: Session) -> list[dict]:
    """Плоская таблица «раунд × участник» — основа CSV-экспорта.

    В выгрузку попадают только раскрытые (исполненные) раунды: экспорт не должен
    становиться обходным путём подсмотреть чужое решение до фиксации.
    """
    rows: list[dict] = []
    for round_row in db.scalars(
        select(Round).where(Round.status.in_(stats_service.REVEALED_STATUSES)).order_by(Round.id)
    ):
        market = db.get(Market, round_row.market_id)
        for decision in db.scalars(
            select(Decision).where(Decision.round_id == round_row.id).order_by(Decision.id)
        ):
            participant = db.get(Participant, decision.participant_id)
            order = db.scalar(
                select(SimulatedOrder).where(SimulatedOrder.decision_id == decision.id)
            )
            payload = decision.payload or {}
            rows.append(
                {
                    "round_id": round_row.id,
                    "round_status": round_row.status,
                    "phase": round_row.phase,
                    "created_at": round_row.created_at.isoformat() if round_row.created_at else "",
                    "market": market.title if market else "",
                    "market_type": market.market_type if market else "",
                    "team_a": market.team_a if market else "",
                    "team_b": market.team_b if market else "",
                    "participant": participant.key if participant else "",
                    "decision_status": decision.status,
                    "action": decision.action or "",
                    "estimated_probability": decision.estimated_probability,
                    "market_probability": decision.market_probability,
                    "edge": decision.edge,
                    "stake_usdc": decision.stake_usdc,
                    "max_acceptable_price": decision.max_acceptable_price,
                    "confidence": decision.confidence,
                    "short_reason": payload.get("short_reason", ""),
                    "key_factors": " | ".join(payload.get("key_factors", []) or []),
                    "risk_factors": " | ".join(payload.get("risk_factors", []) or []),
                    "model_name": decision.model_name or "",
                    "prompt_version": decision.prompt_version or "",
                    "order_status": order.status if order else "",
                    "filled_size": order.filled_size if order else "",
                    "avg_fill_price": order.avg_fill_price if order else "",
                    "notional": order.notional if order else "",
                    "fee": order.fee if order else "",
                    "slippage_bps": order.slippage_bps if order else "",
                }
            )
    return rows


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def full_payload(db: Session) -> dict:
    board = stats_service.scoreboard(db)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "Титан vs Codex vs Claude — The International (paper trading)",
        "mode": "PAPER_TRADING_ONLY",
        "scoreboard": board,
        "rounds": rounds_table(db),
        "biggest_moves": stats_service.biggest_moves(db),
        "disagreements": stats_service.disagreements(db),
        "daily_scoreboard": daily_scoreboard(db),
    }


def daily_scoreboard(db: Session) -> list[dict]:
    """Динамика банков по дням — из журнала операций."""
    from app.db.models import LedgerEntry

    per_day: dict[str, dict[str, float]] = {}
    for participant in db.scalars(select(Participant).order_by(Participant.id)):
        entries = list(
            db.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.participant_id == participant.id)
                .order_by(LedgerEntry.id)
            )
        )
        for e in entries:
            if not e.created_at:
                continue
            day = e.created_at.date().isoformat()
            per_day.setdefault(day, {})[participant.key] = round(e.equity_after, 2)
    return [{"date": day, **values} for day, values in sorted(per_day.items())]


# ---------------------------------------------------------------------------
def equity_chart_svg(db: Session) -> str:
    """График банков в виде самодостаточного SVG (без внешних библиотек)."""
    board = stats_service.scoreboard(db)
    series = {p["participant"]: [pt["equity"] for pt in p["equity_curve"]] for p in board["participants"]}
    if not series:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"

    max_len = max((len(v) for v in series.values()), default=1)
    values = [v for vals in series.values() for v in vals] or [0.0]
    lo, hi = min(values), max(values)
    if abs(hi - lo) < 1e-6:
        lo, hi = lo - 10, hi + 10
    pad = 44

    def x(i: int) -> float:
        return pad + (CHART_W - 2 * pad) * (i / max(max_len - 1, 1))

    def y(v: float) -> float:
        return CHART_H - pad - (CHART_H - 2 * pad) * ((v - lo) / (hi - lo))

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {CHART_W} {CHART_H}' "
        f"width='100%' role='img' aria-label='Динамика банков участников'>",
        f"<rect width='{CHART_W}' height='{CHART_H}' fill='#ffffff'/>",
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1):
        gv = lo + (hi - lo) * frac
        gy = y(gv)
        parts.append(
            f"<line x1='{pad}' y1='{gy:.1f}' x2='{CHART_W - pad}' y2='{gy:.1f}' "
            f"stroke='#e6e6e6' stroke-width='1'/>"
        )
        parts.append(
            f"<text x='6' y='{gy + 4:.1f}' font-size='11' fill='#666'>{gv:.0f}</text>"
        )

    for idx, (key, vals) in enumerate(series.items()):
        if not vals:
            continue
        color = PALETTE.get(key, "#888888")
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
        parts.append(
            f"<polyline fill='none' stroke='{color}' stroke-width='2.5' points='{points}'/>"
        )
        ly = 18 + idx * 18
        parts.append(f"<rect x='{CHART_W - 150}' y='{ly - 9}' width='12' height='12' fill='{color}'/>")
        parts.append(
            f"<text x='{CHART_W - 132}' y='{ly + 1}' font-size='12' fill='#333'>"
            f"{html.escape(key)} — {vals[-1]:.2f}$</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:32px;
      background:#f6f7f9;color:#1c1f23}}
 h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:18px;margin:32px 0 12px}}
 .sub{{color:#6b7280;margin-bottom:24px}}
 .card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:20px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #eceff3;padding:8px 10px;text-align:left}}
 th{{background:#fafbfc;font-weight:600}}
 .pos{{color:#178a4c;font-weight:600}} .neg{{color:#c0392b;font-weight:600}}
 .badge{{display:inline-block;padding:2px 8px;border-radius:99px;background:#eef2ff;
         color:#3730a3;font-size:12px}}
 .note{{font-size:12px;color:#6b7280}}
</style></head><body>
<h1>{title}</h1>
<div class="sub">Сформировано {generated} · режим: <span class="badge">PAPER TRADING</span> ·
реальные сделки не совершаются</div>
{body}
</body></html>
"""


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    cls = "pos" if value > 0 else ("neg" if value < 0 else "")
    return f"<span class='{cls}'>{value:+,.2f}</span>" if cls else f"{value:,.2f}"


def html_report(db: Session) -> str:
    data = full_payload(db)
    board = data["scoreboard"]
    parts: list[str] = []

    # scoreboard
    rows = "".join(
        "<tr>"
        f"<td><b>{html.escape(p['display_name'])}</b> <span class='note'>({p['participant']})</span></td>"
        f"<td>{p['equity']:,.2f}</td>"
        f"<td>{_fmt_money(p['pnl_abs'])}</td>"
        f"<td>{p['pnl_pct']:+.2f}%</td>"
        f"<td>{p['bets_count']}</td>"
        f"<td>{('%.0f%%' % (p['win_rate'] * 100)) if p['win_rate'] is not None else '—'}</td>"
        f"<td>{p['max_drawdown']:,.2f}</td>"
        f"<td>{p['forecast']['brier'] if p['forecast']['brier'] is not None else '—'}</td>"
        f"<td>{p['forecast']['log_loss'] if p['forecast']['log_loss'] is not None else '—'}</td>"
        "</tr>"
        for p in board["participants"]
    )
    parts.append(
        "<div class='card'><h2>Scoreboard</h2><table>"
        "<tr><th>Участник</th><th>Банк</th><th>PnL</th><th>PnL %</th><th>Ставок</th>"
        "<th>Win rate</th><th>Просадка</th><th>Brier</th><th>Log loss</th></tr>"
        f"{rows}</table></div>"
    )

    w = board["winners"]
    parts.append(
        "<div class='card'><h2>Победители</h2>"
        f"<p>🏆 Лучший трейдер: <b>{w['best_trader']['participant'] or '—'}</b> "
        f"({w['best_trader']['equity'] or 0:,.2f} USDC)</p>"
        f"<p>🎯 Лучший прогнозист: <b>{w['best_forecaster']['participant'] or '—'}</b> "
        f"(Brier {w['best_forecaster']['brier'] if w['best_forecaster']['brier'] is not None else '—'})</p>"
        "</div>"
    )

    parts.append(f"<div class='card'><h2>Динамика банков</h2>{equity_chart_svg(db)}</div>")

    # крупнейшие движения
    def moves_table(items: list[dict]) -> str:
        if not items:
            return "<p class='note'>нет данных</p>"
        body = "".join(
            f"<tr><td>{html.escape(i['participant'])}</td><td>{html.escape(i['market'])}</td>"
            f"<td>{i['outcome']}</td><td>{_fmt_money(i['realized_pnl'])}</td></tr>"
            for i in items
        )
        return (
            "<table><tr><th>Участник</th><th>Рынок</th><th>Исход</th><th>PnL</th></tr>"
            f"{body}</table>"
        )

    moves = data["biggest_moves"]
    parts.append(
        "<div class='card'><h2>Крупнейшие выигрыши</h2>"
        + moves_table(moves["biggest_wins"])
        + "<h2>Крупнейшие проигрыши</h2>"
        + moves_table(moves["biggest_losses"])
        + "</div>"
    )

    # расхождения
    dis_rows = "".join(
        f"<tr><td>#{d['round_id']}</td><td>{html.escape(d['market'])}</td>"
        f"<td>{d['phase']}</td><td>{d['probability_spread']:.3f}</td>"
        f"<td>{html.escape(json.dumps(d['probabilities'], ensure_ascii=False))}</td>"
        f"<td>{html.escape(json.dumps(d['actions'], ensure_ascii=False))}</td></tr>"
        for d in data["disagreements"]
    )
    parts.append(
        "<div class='card'><h2>Где участники разошлись сильнее всего</h2><table>"
        "<tr><th>Раунд</th><th>Рынок</th><th>Фаза</th><th>Разброс</th>"
        "<th>Вероятности</th><th>Действия</th></tr>"
        f"{dis_rows or '<tr><td colspan=6 class=note>нет данных</td></tr>'}</table></div>"
    )

    # объяснения решений
    reason_rows = "".join(
        f"<tr><td>#{r['round_id']}</td><td>{html.escape(str(r['participant']))}</td>"
        f"<td>{html.escape(str(r['action']))}</td>"
        f"<td>{r['stake_usdc'] if r['stake_usdc'] is not None else '—'}</td>"
        f"<td>{html.escape(str(r['short_reason']))}</td></tr>"
        for r in data["rounds"]
    )
    parts.append(
        "<div class='card'><h2>Краткие объяснения решений</h2><table>"
        "<tr><th>Раунд</th><th>Участник</th><th>Действие</th><th>Ставка</th><th>Обоснование</th></tr>"
        f"{reason_rows or '<tr><td colspan=5 class=note>нет данных</td></tr>'}</table></div>"
    )

    return _HTML_TEMPLATE.format(
        title="Титан vs Codex vs Claude — отчёт эксперимента",
        generated=data["generated_at"],
        body="".join(parts),
    )


# ---------------------------------------------------------------------------
def write_all(db: Session) -> dict[str, str]:
    """Сохранить полный комплект файлов и вернуть пути."""
    out = export_dir()
    stamp = _timestamp()
    files: dict[str, str] = {}

    csv_path = out / f"rounds-{stamp}.csv"
    csv_path.write_text(to_csv(rounds_table(db)), encoding="utf-8")
    files["csv"] = str(csv_path)

    json_path = out / f"experiment-{stamp}.json"
    json_path.write_text(
        json.dumps(full_payload(db), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    files["json"] = str(json_path)

    html_path = out / f"report-{stamp}.html"
    html_path.write_text(html_report(db), encoding="utf-8")
    files["html"] = str(html_path)

    svg_path = out / f"equity-{stamp}.svg"
    svg_path.write_text(equity_chart_svg(db), encoding="utf-8")
    files["svg"] = str(svg_path)

    daily_path = out / f"daily-scoreboard-{stamp}.csv"
    daily = daily_scoreboard(db)
    daily_path.write_text(to_csv(daily) if daily else "", encoding="utf-8")
    files["daily_csv"] = str(daily_path)

    return files
