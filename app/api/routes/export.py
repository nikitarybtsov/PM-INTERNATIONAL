"""Экспорт для монтажа: CSV, JSON, HTML-отчёт, SVG-график.

Ничего никуда не публикуется — только отдача файлов и запись на диск.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.services import export as export_service

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/json")
def export_json(db: Session = Depends(get_db)) -> dict:
    return export_service.full_payload(db)


@router.get("/csv", response_class=PlainTextResponse)
def export_csv(db: Session = Depends(get_db)) -> PlainTextResponse:
    csv_text = export_service.to_csv(export_service.rounds_table(db))
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=rounds.csv"},
    )


@router.get("/daily-scoreboard", response_class=PlainTextResponse)
def export_daily(db: Session = Depends(get_db)) -> PlainTextResponse:
    rows = export_service.daily_scoreboard(db)
    return PlainTextResponse(
        export_service.to_csv(rows) if rows else "",
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=daily-scoreboard.csv"},
    )


@router.get("/report", response_class=HTMLResponse)
def export_report(db: Session = Depends(get_db)) -> HTMLResponse:
    return HTMLResponse(export_service.html_report(db))


@router.get("/chart.svg")
def export_chart(db: Session = Depends(get_db)) -> Response:
    return Response(export_service.equity_chart_svg(db), media_type="image/svg+xml")


@router.post("/write")
def write_files(db: Session = Depends(get_db)) -> dict:
    """Сохранить весь комплект в EXPORT_DIR и вернуть пути к файлам."""
    return {"files": export_service.write_all(db), "published": False}
