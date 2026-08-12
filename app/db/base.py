"""Подключение к БД: SQLite по умолчанию, PostgreSQL через DATABASE_URL."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # timeout — сколько ждать освобождения блокировки, прежде чем сдаться.
        # Умолчание 5 секунд: анализ моделей идёт минутами, и параллельная
        # запись падала с «database is locked», а оператор видел 500.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 60}
        # каталог для файла БД
        if ":///" in url and not url.endswith(":memory:"):
            db_path = Path(url.split(":///", 1)[1])
            if db_path.parent and str(db_path.parent) not in ("", "."):
                db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)  # type: ignore[arg-type]
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - тривиально
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            # WAL: читатели не блокируют писателя и наоборот. Без него любой
            # фоновый анализ вешал панель целиком.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=60000")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def configure_engine(url: str) -> Engine:
    """Пересоздать движок (используется в тестах)."""
    global _engine, _SessionLocal
    _engine = _make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_db() -> Generator[Session, None, None]:
    """FastAPI-зависимость."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    from app.db import models  # noqa: F401  — регистрация моделей

    Base.metadata.create_all(bind=get_engine())
