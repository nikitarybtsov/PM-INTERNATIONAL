"""Реальное исполнение: предохранители, ограничения биржи, разбор ответа.

Ни один тест не ходит в сеть: клиент CLOB подменяется заглушкой. Проверяется
главное — что без трёх независимых разрешений ордер не уходит.
"""

from __future__ import annotations

import pytest

from app.adapters.execution import (
    ExecutionError,
    NotApproved,
    OrderRequest,
    OrderStatus,
    PolymarketLiveAdapter,
)
from app.adapters.execution.polymarket_live import round_price_to_tick
from app.config import reset_settings_cache

WALLET_ENV = {
    "CODEX_POLY_PRIVATE_KEY": "0x" + "11" * 32,
    "CODEX_POLY_FUNDER": "0x1111111111111111111111111111111111111111",
    "CODEX_POLY_SIGNATURE_TYPE": "3",
    "CLAUDE_POLY_PRIVATE_KEY": "0x" + "22" * 32,
    "CLAUDE_POLY_FUNDER": "0x2222222222222222222222222222222222222222",
    "CLAUDE_POLY_SIGNATURE_TYPE": "1",
}


class FakeClient:
    """Заглушка CLOB: запоминает вызовы, ничего не подписывает и не шлёт."""

    def __init__(self, response=None, raises=None):
        self.response = response or {"success": True, "orderID": "0xabc",
                                     "takingAmount": "100", "price": "0.70"}
        self.raises = raises
        self.calls: list[dict] = []

    def create_and_post_market_order(self, args=None, **kwargs):
        # SDK принимает объект аргументов позиционно; kwargs остались для
        # order_type. Разворачиваем в плоский словарь, чтобы проверки в тестах
        # читались так же, как раньше.
        payload = dict(kwargs)
        for field in ("token_id", "amount", "price", "side", "order_type"):
            value = getattr(args, field, None)
            if value is not None:
                payload[field] = value
        self.calls.append(payload)
        if self.raises:
            raise self.raises
        return self.response


@pytest.fixture
def live_env(monkeypatch):
    """Боевой режим со связками кошельков, но всё ещё в dry-run."""
    for key, value in WALLET_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_DRY_RUN", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


def order(**kwargs) -> OrderRequest:
    params = {
        "participant": "codex",
        "market_id": 1,
        "token_id": "tok-yes",
        "outcome": "YES",
        "size": 100.0,
        "max_price": 0.70,
        "approved_by": "operator",
    }
    params.update(kwargs)
    return OrderRequest(**params)


# --- предохранитель 1: общий флаг ------------------------------------------
def test_refuses_when_live_trading_disabled(monkeypatch):
    for key, value in WALLET_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    reset_settings_cache()

    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    with pytest.raises(ExecutionError, match="боевой режим выключен"):
        adapter.execute(order())
    reset_settings_cache()


# --- предохранитель 2: одобрение оператора ---------------------------------
def test_refuses_without_operator_approval(live_env):
    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    with pytest.raises(NotApproved):
        adapter.execute(order(approved_by=None))


def test_refuses_with_empty_approval(live_env):
    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    with pytest.raises(NotApproved):
        adapter.execute(order(approved_by=""))


# --- предохранитель 3: dry-run ---------------------------------------------
def test_dry_run_does_not_touch_exchange(live_env):
    client = FakeClient()
    adapter = PolymarketLiveAdapter(client_factory=lambda w: client)

    result = adapter.execute(order())

    assert result.status is OrderStatus.DRY_RUN
    assert result.dry_run is True
    assert client.calls == []  # к бирже не обращались
    assert result.fee_usdc == pytest.approx(100 * 0.05 * 0.70 * 0.30)


def test_real_order_sent_only_when_dry_run_disabled(live_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    reset_settings_cache()
    client = FakeClient()
    adapter = PolymarketLiveAdapter(client_factory=lambda w: client)

    result = adapter.execute(order())

    assert len(client.calls) == 1
    assert client.calls[0]["side"] == "BUY"
    assert client.calls[0]["order_type"] == "FAK"
    assert result.status is OrderStatus.FILLED
    assert result.order_id == "0xabc"


# --- ограничения биржи ------------------------------------------------------
def test_rejects_order_below_exchange_minimum(live_env):
    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    # 5 контрактов по 0.70 = 3.50 USDC, минимум биржи 5.00
    with pytest.raises(ExecutionError, match="меньше минимума"):
        adapter.execute(order(size=5, max_price=0.70))


def test_rejects_invalid_price(live_env):
    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    for bad in (0.0, 1.0, 1.5, -0.2):
        with pytest.raises(ExecutionError, match="недопустимая цена"):
            adapter.execute(order(max_price=bad))


def test_rejects_zero_size(live_env):
    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    with pytest.raises(ExecutionError, match="больше нуля"):
        adapter.execute(order(size=0))


def test_price_rounds_down_to_tick():
    """Округление вверх нарушило бы потолок цены участника."""
    assert round_price_to_tick(0.7049, 0.01) == pytest.approx(0.70)
    assert round_price_to_tick(0.709, 0.01) == pytest.approx(0.70)
    assert round_price_to_tick(0.70, 0.01) == pytest.approx(0.70)


def test_missing_wallet_is_reported(live_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    monkeypatch.delenv("CLAUDE_POLY_PRIVATE_KEY", raising=False)
    reset_settings_cache()
    adapter = PolymarketLiveAdapter(client_factory=lambda w: FakeClient())
    with pytest.raises(ExecutionError, match="не заданы ключи"):
        adapter.execute(order(participant="claude"))


# --- разбор ответа биржи ----------------------------------------------------
def test_partial_fill_is_recognised(live_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    reset_settings_cache()
    client = FakeClient(response={"success": True, "orderID": "0x1",
                                  "takingAmount": "40", "price": "0.71"})
    adapter = PolymarketLiveAdapter(client_factory=lambda w: client)

    result = adapter.execute(order(size=100))

    assert result.status is OrderStatus.PARTIAL
    assert result.filled_size == pytest.approx(40)
    assert result.avg_price == pytest.approx(0.71)
    assert result.fee_usdc == pytest.approx(40 * 0.05 * 0.71 * 0.29)


def test_exchange_rejection_is_not_an_exception(live_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    reset_settings_cache()
    client = FakeClient(response={"success": False, "errorMsg": "not enough balance"})
    adapter = PolymarketLiveAdapter(client_factory=lambda w: client)

    result = adapter.execute(order())

    assert result.status is OrderStatus.REJECTED
    assert "not enough balance" in result.error
    assert result.is_success is False


def test_network_failure_is_captured(live_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    reset_settings_cache()
    client = FakeClient(raises=TimeoutError("нет связи"))
    adapter = PolymarketLiveAdapter(client_factory=lambda w: client)

    result = adapter.execute(order())

    assert result.status is OrderStatus.FAILED
    assert "TimeoutError" in result.error


def test_zero_fill_counts_as_rejected(live_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    reset_settings_cache()
    client = FakeClient(response={"success": True, "orderID": "0x2", "takingAmount": "0"})
    adapter = PolymarketLiveAdapter(client_factory=lambda w: client)

    assert adapter.execute(order()).status is OrderStatus.REJECTED


# --- конфигурация кошельков -------------------------------------------------
def test_wallets_have_independent_signature_types(live_env):
    from app.config import get_settings

    settings = get_settings()
    codex = settings.wallet_for("codex")
    claude = settings.wallet_for("claude")
    assert codex.signature_type == 3
    assert claude.signature_type == 1
    assert codex.funder != claude.funder


def test_wallet_never_reveals_private_key_in_repr(live_env):
    from app.config import get_settings

    wallet = get_settings().wallet_for("codex")
    assert "***" in repr(wallet)
    assert wallet.private_key not in repr(wallet)
    assert wallet.private_key not in str(wallet)


def test_live_execution_ready_requires_all_three(live_env, monkeypatch):
    from app.config import get_settings

    # dry-run включён — боевого исполнения нет
    assert get_settings().live_execution_ready() is False

    monkeypatch.setenv("EXECUTION_DRY_RUN", "false")
    reset_settings_cache()
    assert get_settings().live_execution_ready() is True

    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    reset_settings_cache()
    assert get_settings().live_execution_ready() is False
