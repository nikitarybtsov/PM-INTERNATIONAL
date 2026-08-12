"""Точка входа FastAPI.

ВНИМАНИЕ: приложение работает исключительно в режиме paper trading.
Никаких реальных ордеров, транзакций, приватных ключей и подписи операций.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import LIVE_TRADING_ENABLED, get_settings
from app.db.base import create_all, session_scope
from app.services.seed import seed_participants

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

DESCRIPTION = """
Публичный эксперимент: **Титан (человек) против Codex и Claude** на рынках
The International (Polymarket).

* Режим: **PAPER TRADING**. Реальные сделки физически не поддерживаются.
* У каждого участника отдельный виртуальный банк $1000 USDC.
* Все решения принимаются по одному и тому же immutable snapshot.
* Решения скрыты друг от друга до исполнения раунда.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    create_all()
    with session_scope() as db:
        seed_participants(db)
    logger.info(
        "старт: провайдер=%s, live_trading=%s", settings.market_data_provider, LIVE_TRADING_ENABLED
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="PM-INTERNATIONAL — Титан vs Codex vs Claude",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )

    from app.api.routes import export, markets, portfolios, rounds, stats, ui

    app.include_router(markets.router)
    app.include_router(rounds.router)
    app.include_router(portfolios.router)
    app.include_router(stats.router)
    app.include_router(export.router)
    app.include_router(ui.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/health", tags=["system"])
    def health() -> dict:
        settings = get_settings()
        return {
            "status": "ok",
            "mode": "PAPER_TRADING_ONLY",
            "live_trading_enabled": LIVE_TRADING_ENABLED,
            "market_data_provider": settings.market_data_provider,
            "participants_in_mock_mode": {
                "codex": not settings.has_openai(),
                "claude": not settings.has_anthropic(),
            },
        }

    @app.get("/api/config", tags=["system"])
    def config() -> dict:
        """Конфигурация без секретов."""
        return get_settings().safe_dump()

    return app


app = create_app()
