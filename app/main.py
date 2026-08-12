"""Точка входа FastAPI.

ВНИМАНИЕ: приложение работает исключительно в режиме paper trading.
Никаких реальных ордеров, транзакций, приватных ключей и подписи операций.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.auth import auth_middleware
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


async def _scheduler_loop(stop: asyncio.Event) -> None:
    """Фоновый цикл планировщика. Тик выполняется в отдельном потоке."""
    from app.services import scheduler as scheduler_service

    settings = get_settings()
    interval = max(30, settings.scheduler_interval_seconds)
    logger.info("планировщик запущен, интервал %sс", interval)

    def _run_tick() -> dict:
        with session_scope() as db:
            return scheduler_service.tick(db).as_dict()

    while not stop.is_set():
        try:
            result = await asyncio.to_thread(_run_tick)
            if any(result[k] for k in ("rounds_opened", "rounds_executed", "rounds_cancelled")):
                logger.info("тик планировщика: %s", result)
            for error in result.get("errors", []):
                logger.warning("планировщик: %s", error)
        except Exception:  # noqa: BLE001 — цикл не имеет права умереть
            logger.exception("сбой тика планировщика")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


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
        "старт: провайдер=%s, codex=%s, live_trading=%s",
        settings.market_data_provider,
        settings.codex_transport,
        LIVE_TRADING_ENABLED,
    )
    if not settings.panel_auth_enabled():
        logger.warning(
            "PANEL_PASSWORD не задан — панель открыта без авторизации. "
            "Обязательно задайте пароль, если панель доступна из интернета."
        )

    stop = asyncio.Event()
    task: asyncio.Task | None = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(_scheduler_loop(stop))

    with contextlib.suppress(Exception):
        from app.services import notifications

        with session_scope() as db:
            notifications.startup(db)

    yield

    stop.set()
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="PM-INTERNATIONAL — Титан vs Codex vs Claude",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )

    app.middleware("http")(auth_middleware)

    from app.api.routes import export, markets, portfolios, rounds, scheduler, stats, ui

    app.include_router(markets.router)
    app.include_router(rounds.router)
    app.include_router(portfolios.router)
    app.include_router(stats.router)
    app.include_router(export.router)
    app.include_router(scheduler.router)
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
            "codex_transport": settings.codex_transport,
            "participants_in_mock_mode": {
                "codex": settings.codex_transport == "api" and not settings.has_openai(),
                "claude": not settings.has_anthropic(),
            },
            "telegram_configured": settings.has_telegram(),
            "panel_auth_enabled": settings.panel_auth_enabled(),
            "scheduler_enabled": settings.scheduler_enabled,
        }

    @app.get("/api/config", tags=["system"])
    def config() -> dict:
        """Конфигурация без секретов."""
        return get_settings().safe_dump()

    @app.get("/api/doctor", tags=["system"])
    def doctor() -> dict:
        """Проверка готовности внешних зависимостей перед запуском."""
        from app.adapters.participants.codex_cli import CodexCliAdapter

        settings = get_settings()
        checks: dict[str, object] = {
            "claude_api_key": settings.has_anthropic(),
            "telegram": settings.has_telegram(),
            "panel_password": settings.panel_auth_enabled(),
            "titan_token": bool(settings.titan_access_token),
            "market_provider": settings.market_data_provider,
        }
        if settings.codex_transport == "cli":
            checks["codex_cli"] = CodexCliAdapter.probe()
        else:
            checks["codex_api_key"] = settings.has_openai()

        problems = []
        if not checks.get("claude_api_key"):
            problems.append("нет ANTHROPIC_API_KEY — Claude работает заглушкой")
        if settings.codex_transport == "cli":
            probe = checks.get("codex_cli") or {}
            if not probe.get("available"):
                problems.append(f"Codex CLI недоступен: {probe.get('error')}")
        elif not checks.get("codex_api_key"):
            problems.append("нет OPENAI_API_KEY — Codex работает заглушкой")
        if not checks["telegram"]:
            problems.append("Telegram не настроен — уведомлений не будет")
        if settings.market_data_provider == "mock":
            problems.append("источник рынков — mock, живых котировок нет")

        return {"checks": checks, "problems": problems, "ready": not problems}

    return app


app = create_app()
