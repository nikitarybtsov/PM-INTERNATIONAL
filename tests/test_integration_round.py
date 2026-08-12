"""Интеграционный тест: полный paper-trading раунд через HTTP API.

Проходит весь сценарий готовности проекта:
seed → рынок → snapshot → Codex/Claude → Titan → risk engine → paper execution →
раскрытие → settlement → обновление scoreboard.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_full_paper_trading_round(client: TestClient):
    # 1. seed участников и демо-рынков
    seeded = client.post("/api/markets/seed", json={"with_markets": True}).json()
    assert set(seeded["participants"]) == {"codex", "claude", "titan"}
    assert seeded["markets"]

    market_id = seeded["markets"][0]["id"]

    # стартовые банки
    portfolios = client.get("/api/portfolios").json()
    assert len(portfolios) == 3
    assert all(p["cash_balance"] == 1000.0 for p in portfolios)

    # 2. заметки оператора попадут в snapshot
    client.post(f"/api/markets/{market_id}/notes", json={"notes": "Bo3, замен в составах нет"})

    # 3. создаём раунд — фиксируется единый snapshot
    created = client.post(
        "/api/rounds", json={"market_id": market_id, "phase": "PREMATCH"}
    )
    assert created.status_code == 201
    round_id = created.json()["round_id"]

    snapshot = client.get(f"/api/rounds/{round_id}/snapshot").json()
    assert snapshot["stale"] is False
    assert snapshot["payload"]["operator_context"] == "Bo3, замен в составах нет"
    assert snapshot["payload"]["yes_book"]["asks"]

    # 4. решения ИИ (mock-режим, ключи не нужны)
    ai = client.post(f"/api/rounds/{round_id}/request-ai").json()
    assert set(ai["collected"]) == {"codex", "claude"}
    assert all(status == "VALID" for status in ai["statuses"].values())

    # 5. до подачи решения Титана всё скрыто
    hidden = client.get(f"/api/rounds/{round_id}/decisions").json()
    assert hidden["hidden"] is True
    assert hidden["awaiting"] == ["titan"]

    # 6. Титан вводит решение вручную
    submitted = client.post(
        f"/api/rounds/{round_id}/decisions/titan",
        json={
            "action": "BUY_YES",
            "estimated_probability": 0.72,
            "stake_usdc": 80,
            "max_acceptable_price": 0.95,
            "confidence": 0.85,
            "short_reason": "фаворит недооценён рынком",
            "key_factors": ["форма команды", "удобный патч"],
            "risk_factors": ["возможна замена игрока"],
            "information_used": ["snapshot", "личный опыт ранга Титан"],
        },
    )
    assert submitted.status_code == 200
    assert submitted.json()["locked"] is True

    # повторная отправка запрещена
    again = client.post(
        f"/api/rounds/{round_id}/decisions/titan",
        json={
            "action": "HOLD",
            "estimated_probability": 0.5,
            "stake_usdc": 0,
            "confidence": 0.5,
            "short_reason": "передумал",
        },
    )
    assert again.status_code == 409

    # 7. решения по-прежнему скрыты, но раунд заперт
    state = client.get(f"/api/rounds/{round_id}").json()
    assert state["status"] == "LOCKED"
    assert client.get(f"/api/rounds/{round_id}/decisions").json()["hidden"] is True

    # 8. risk engine + paper execution
    executed = client.post(f"/api/rounds/{round_id}/execute")
    assert executed.status_code == 200
    report = executed.json()["report"]
    assert set(report) == {"codex", "claude", "titan"}
    for entry in report.values():
        assert entry["reasons"]
    assert report["titan"]["executed"] is True
    assert report["titan"]["filled_size"] > 0

    # повторное исполнение блокируется
    assert client.post(f"/api/rounds/{round_id}/execute").status_code == 409

    # 9. раскрытие решений
    revealed = client.post(f"/api/rounds/{round_id}/reveal").json()
    assert revealed["hidden"] is False
    assert len(revealed["decisions"]) == 3
    titan_entry = next(d for d in revealed["decisions"] if d["participant"] == "titan")
    assert titan_entry["decision"]["short_reason"] == "фаворит недооценён рынком"
    assert titan_entry["risk"]["verdict"] in ("APPROVED", "ADJUSTED")
    assert titan_entry["order"]["status"] in ("FILLED", "PARTIALLY_FILLED")

    # 10. банк Титана уменьшился на потраченную сумму
    portfolios = {p["participant"]: p for p in client.get("/api/portfolios").json()}
    assert portfolios["titan"]["cash_balance"] < 1000.0
    assert portfolios["titan"]["open_positions_count"] == 1

    positions = client.get("/api/portfolios/titan/positions").json()
    assert positions[0]["outcome"] == "YES"
    assert positions[0]["size"] > 0

    # 11. результат рынка выставляет оператор
    settled = client.post(f"/api/markets/{market_id}/settle", json={"winning_outcome": "YES"})
    assert settled.status_code == 200
    assert client.post(
        f"/api/markets/{market_id}/settle", json={"winning_outcome": "NO"}
    ).status_code == 409

    # 12. scoreboard и PnL обновились
    board = client.get("/api/stats/scoreboard").json()
    titan_stats = next(p for p in board["participants"] if p["participant"] == "titan")
    assert titan_stats["equity"] > 1000.0
    assert titan_stats["realized_pnl"] > 0
    assert titan_stats["wins"] == 1
    assert titan_stats["forecast"]["brier"] is not None
    assert board["winners"]["best_trader"]["participant"] is not None
    assert board["winners"]["best_forecaster"]["participant"] is not None

    # 13. аудит зафиксировал ключевые события
    audit = client.get("/api/stats/audit").json()
    actions = {e["action"] for e in audit}
    assert {"create", "lock", "execute", "reveal", "submit", "settle"} & actions

    # 14. экспорт формируется
    assert client.get("/api/export/csv").status_code == 200
    assert client.get("/api/export/report").status_code == 200
    files = client.post("/api/export/write").json()
    assert files["published"] is False
    assert set(files["files"]) >= {"csv", "json", "html", "svg"}


def test_between_maps_flow_invalidates_previous_snapshot(client: TestClient):
    seeded = client.post("/api/markets/seed", json={"with_markets": True}).json()
    market_id = seeded["markets"][0]["id"]

    first = client.post("/api/rounds", json={"market_id": market_id, "phase": "PREMATCH"}).json()
    round_id = first["round_id"]
    client.post(f"/api/rounds/{round_id}/request-ai")
    client.post(
        f"/api/rounds/{round_id}/decisions/titan",
        json={
            "action": "BUY_YES",
            "estimated_probability": 0.7,
            "stake_usdc": 50,
            "max_acceptable_price": 0.95,
            "confidence": 0.8,
            "short_reason": "ставлю до старта",
        },
    )
    # раунд ещё не исполнен, но оператор подтверждает окончание карты
    client.post(f"/api/rounds/{round_id}/execute")

    second = client.post(
        f"/api/rounds/between-maps?market_id={market_id}",
        json={"map_number": 1, "operator_context": "счёт 1:0"},
    )
    assert second.status_code == 201
    assert second.json()["phase"] == "BETWEEN_MAPS"

    old_snapshot = client.get(f"/api/rounds/{round_id}/snapshot").json()
    assert old_snapshot["stale"] is True
    assert "заменён" in old_snapshot["stale_reason"]


def test_ui_pages_render(client: TestClient):
    seeded = client.post("/api/markets/seed", json={"with_markets": True}).json()
    market_id = seeded["markets"][0]["id"]
    round_id = client.post(
        "/api/rounds", json={"market_id": market_id, "phase": "PREMATCH"}
    ).json()["round_id"]

    for path in ("/", "/ui/markets", "/ui/exports", f"/ui/rounds/{round_id}",
                 f"/ui/rounds/{round_id}/titan"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "PAPER TRADING" in response.text


def test_health_and_config_expose_no_secrets(client: TestClient):
    health = client.get("/health").json()
    assert health["live_trading_enabled"] is False
    assert health["mode"] == "PAPER_TRADING_ONLY"

    config = client.get("/api/config").json()
    assert config["live_trading_enabled"] is False
    for key, value in config.items():
        if "api_key" in key:
            assert value in (None, "***set***")
    assert "sk-" not in str(config)
