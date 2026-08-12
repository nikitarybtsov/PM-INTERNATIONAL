"""Общие фикстуры: изолированная БД в файле tmp, mock-провайдеры, seed."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Тесты никогда не должны видеть настоящие ключи из окружения разработчика.
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["MARKET_DATA_PROVIDER"] = "mock"

from app.adapters.market_data.factory import set_provider_override  # noqa: E402
from app.adapters.market_data.mock import MockMarketDataProvider  # noqa: E402
from app.adapters.participants.factory import clear_adapter_overrides  # noqa: E402
from app.config import reset_settings_cache  # noqa: E402
from app.db.base import Base, configure_engine, get_sessionmaker  # noqa: E402
from app.db.models import Market  # noqa: E402
from app.services.seed import seed_participants  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "mock")
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    reset_settings_cache()

    engine = configure_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    set_provider_override(MockMarketDataProvider())
    clear_adapter_overrides()
    yield
    set_provider_override(None)
    clear_adapter_overrides()
    reset_settings_cache()


@pytest.fixture
def db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def seeded(db: Session):
    """Три участника с банками по $1000 и один демо-рынок."""
    from app.services.seed import seed_markets

    participants = seed_participants(db)
    markets = seed_markets(db)
    db.commit()
    return {"participants": participants, "markets": markets}


@pytest.fixture
def market(db: Session, seeded) -> Market:
    return seeded["markets"][0]


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
