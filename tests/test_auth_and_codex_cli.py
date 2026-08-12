"""Тесты авторизации панели и адаптера Codex через CLI."""

from __future__ import annotations

import base64
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.adapters.participants.base import ParticipantError, PortfolioView
from app.adapters.participants.codex_cli import CodexCliAdapter, CodexCliError
from app.config import get_settings, reset_settings_cache
from app.constants import Action, Phase
from app.schemas.snapshot import BookLevel, MarketSnapshot, SnapshotBook, SnapshotMarketInfo

VIEW = PortfolioView(
    participant_key="codex",
    cash_balance=1000.0,
    reserved_balance=0.0,
    initial_balance=1000.0,
)

VALID_JSON = """
Reading prompt...
{"action": "BUY_YES", "estimated_probability": 0.62, "stake_usdc": 40,
 "max_acceptable_price": 0.6, "confidence": 0.7,
 "short_reason": "рынок недооценивает фаворита"}
Done.
"""


def demo_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id=1,
        phase=Phase.PREMATCH,
        market=SnapshotMarketInfo(
            market_id=1, external_id="m1", source="mock", title="A vs B", team_a="A", team_b="B"
        ),
        yes_price=0.5,
        no_price=0.5,
        yes_book=SnapshotBook(
            bids=[BookLevel(price=0.49, size=2000)], asks=[BookLevel(price=0.51, size=2000)]
        ),
        no_book=SnapshotBook(
            bids=[BookLevel(price=0.49, size=2000)], asks=[BookLevel(price=0.51, size=2000)]
        ),
        liquidity_usdc=5000.0,
    )


# --- Codex CLI --------------------------------------------------------------
def _fake_run(stdout="", stderr="", returncode=0, exc=None):
    def runner(argv, **kwargs):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return runner


def test_cli_adapter_is_not_mock_by_default(monkeypatch):
    monkeypatch.setenv("CODEX_TRANSPORT", "cli")
    reset_settings_cache()
    assert CodexCliAdapter().is_mock is False
    reset_settings_cache()


def test_cli_parses_json_amid_noise(monkeypatch):
    adapter = CodexCliAdapter(mock=False)
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=VALID_JSON))
    result = adapter.decide(demo_snapshot(), VIEW)
    assert result.decision.action == Action.BUY_YES
    assert result.decision.stake_usdc == 40


def test_cli_missing_binary(monkeypatch):
    adapter = CodexCliAdapter(mock=False)
    monkeypatch.setattr(subprocess, "run", _fake_run(exc=FileNotFoundError()))
    with pytest.raises(ParticipantError):
        adapter.decide(demo_snapshot(), VIEW)


def test_cli_nonzero_exit(monkeypatch):
    adapter = CodexCliAdapter(mock=False)
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1, stderr="not logged in"))
    with pytest.raises(ParticipantError):
        adapter.decide(demo_snapshot(), VIEW)


def test_cli_timeout(monkeypatch):
    adapter = CodexCliAdapter(mock=False)
    monkeypatch.setattr(
        subprocess, "run", _fake_run(exc=subprocess.TimeoutExpired(cmd="codex", timeout=1))
    )
    with pytest.raises(ParticipantError):
        adapter.decide(demo_snapshot(), VIEW)


def test_cli_empty_output(monkeypatch):
    adapter = CodexCliAdapter(mock=False)
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="   "))
    with pytest.raises(ParticipantError):
        adapter.decide(demo_snapshot(), VIEW)


def test_cli_retries_then_succeeds(monkeypatch):
    adapter = CodexCliAdapter(mock=False)
    calls = {"n": 0}

    def runner(argv, **kwargs):
        calls["n"] += 1
        out = "мусор без json" if calls["n"] == 1 else VALID_JSON
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", runner)
    result = adapter.decide(demo_snapshot(), VIEW)
    assert result.attempts == 2
    assert calls["n"] == 2


def test_cli_empty_command_is_rejected(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_COMMAND", "")
    reset_settings_cache()
    adapter = CodexCliAdapter(mock=False)
    with pytest.raises(CodexCliError):
        adapter._build_argv()
    reset_settings_cache()


def test_cli_probe_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(exc=FileNotFoundError()))
    probe = CodexCliAdapter.probe()
    assert probe["available"] is False


def test_factory_selects_cli_transport(monkeypatch):
    from app.adapters.participants.factory import get_participant_adapter

    monkeypatch.setenv("CODEX_TRANSPORT", "cli")
    reset_settings_cache()
    assert isinstance(get_participant_adapter("codex"), CodexCliAdapter)
    reset_settings_cache()


# --- авторизация панели -----------------------------------------------------
@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("PANEL_PASSWORD", "s3cret")
    monkeypatch.setenv("PANEL_USER", "operator")
    monkeypatch.setenv("TITAN_ACCESS_TOKEN", "titan-token")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _basic(user: str, password: str) -> dict:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_auth_disabled_when_no_password(client: TestClient):
    assert get_settings().panel_auth_enabled() is False
    assert client.get("/").status_code == 200


def test_panel_requires_auth(client: TestClient, secured):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_basic("operator", "wrong")).status_code == 401
    assert client.get("/", headers=_basic("operator", "s3cret")).status_code == 200


def test_health_stays_public(client: TestClient, secured):
    assert client.get("/health").status_code == 200


def test_titan_page_opens_with_token(client: TestClient, secured):
    seeded = client.post(
        "/api/markets/seed", json={"with_markets": True}, headers=_basic("operator", "s3cret")
    ).json()
    market_id = seeded["markets"][0]["id"]
    round_id = client.post(
        "/api/rounds",
        json={"market_id": market_id, "phase": "PREMATCH"},
        headers=_basic("operator", "s3cret"),
    ).json()["round_id"]

    # без токена — закрыто
    assert client.get(f"/ui/rounds/{round_id}/titan").status_code == 401
    # с токеном — открыто
    assert client.get(f"/ui/rounds/{round_id}/titan?t=titan-token").status_code == 200
    # с неверным токеном — закрыто
    assert client.get(f"/ui/rounds/{round_id}/titan?t=nope").status_code == 401


def test_titan_token_does_not_open_operator_pages(client: TestClient, secured):
    assert client.get("/?t=titan-token").status_code == 401
    assert client.get("/ui/markets?t=titan-token").status_code == 401
    assert client.get("/api/stats/scoreboard?t=titan-token").status_code == 401


def test_titan_can_submit_with_token(client: TestClient, secured):
    seeded = client.post(
        "/api/markets/seed", json={"with_markets": True}, headers=_basic("operator", "s3cret")
    ).json()
    market_id = seeded["markets"][0]["id"]
    round_id = client.post(
        "/api/rounds",
        json={"market_id": market_id, "phase": "PREMATCH"},
        headers=_basic("operator", "s3cret"),
    ).json()["round_id"]

    body = {
        "action": "HOLD",
        "estimated_probability": 0.5,
        "stake_usdc": 0,
        "confidence": 0.5,
        "short_reason": "пропускаю раунд",
    }
    assert client.post(f"/api/rounds/{round_id}/decisions/titan", json=body).status_code == 401
    ok = client.post(f"/api/rounds/{round_id}/decisions/titan?t=titan-token", json=body)
    assert ok.status_code == 200
