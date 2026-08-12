"""Реальное исполнение на Polymarket через CLOB.

Работает поверх `py_clob_client_v2`: подпись ордера делает библиотека, приватный
ключ живёт только в памяти процесса и никогда не покидает её — ни в БД, ни в
логи, ни в экспорт.

Предохранители, проверяемые до обращения к сети:

1. `LIVE_TRADING_ENABLED=true` — иначе исключение;
2. `approved_by` у заявки — без одобрения оператора ордер не уходит;
3. `EXECUTION_DRY_RUN=true` (по умолчанию) — заявка считается и логируется,
   но на биржу не отправляется.

Ограничения биржи, которые нужно соблюдать до отправки, иначе ордер отклонят:
минимальный размер $5 и шаг цены 0.01.
"""

from __future__ import annotations

import logging
import math

from app.adapters.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    NotApproved,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from app.config import WalletConfig, get_settings
from app.services.fees import FeeSchedule, taker_fee

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"


def round_price_to_tick(price: float, tick: float) -> float:
    """Округлить цену вниз до шага биржи — вверх нельзя, это выход за потолок."""
    if tick <= 0:
        return price
    return math.floor(price / tick + 1e-9) * tick


class PolymarketLiveAdapter(ExecutionAdapter):
    name = "polymarket-live"

    def __init__(self, client_factory=None) -> None:
        self._settings = get_settings()
        self._clients: dict[str, object] = {}
        # Подменяется в тестах: настоящий клиент ходит в сеть и подписывает.
        self._client_factory = client_factory or self._build_client

    # ---- клиент ------------------------------------------------------------
    def _build_client(self, wallet: WalletConfig):  # pragma: no cover - нужен SDK и сеть
        from py_clob_client_v2.client import ClobClient

        client = ClobClient(
            host=CLOB_HOST,
            chain_id=wallet.chain_id,
            key=wallet.private_key,
            signature_type=wallet.signature_type,
            funder=wallet.funder,
        )
        client.set_api_creds(client.create_or_derive_api_key())
        return client

    def _client_for(self, participant: str):
        if participant not in self._clients:
            wallet = self._settings.wallet_for(participant)
            if wallet is None:
                raise ExecutionError(
                    f"для участника {participant} не заданы ключи кошелька"
                )
            self._clients[participant] = self._client_factory(wallet)
        return self._clients[participant]

    # ---- проверки перед отправкой -----------------------------------------
    def _guard(self, request: OrderRequest) -> None:
        settings = self._settings
        if not settings.live_trading_enabled:
            raise ExecutionError(
                "боевой режим выключен (LIVE_TRADING_ENABLED=false)"
            )
        if not request.approved_by:
            raise NotApproved(
                f"сделка {request.participant}/{request.market_id} не одобрена оператором"
            )
        if request.size <= 0:
            raise ExecutionError("размер заявки должен быть больше нуля")
        if not (0.0 < request.max_price < 1.0):
            raise ExecutionError(f"недопустимая цена {request.max_price}")
        if request.notional_usdc < settings.polymarket_min_order_usdc:
            raise ExecutionError(
                f"сумма {request.notional_usdc:.2f} USDC меньше минимума биржи "
                f"{settings.polymarket_min_order_usdc:.2f}"
            )

    # ---- публичный интерфейс ----------------------------------------------
    def execute(self, request: OrderRequest) -> OrderResult:
        self._guard(request)
        settings = self._settings
        price = round_price_to_tick(request.max_price, settings.polymarket_price_tick)
        schedule = FeeSchedule(
            rate=settings.polymarket_taker_fee_rate,
            rebate_rate=settings.polymarket_taker_rebate_rate,
        )

        if settings.execution_dry_run:
            fee = taker_fee(request.size, price, schedule)
            logger.info(
                "DRY-RUN %s: %s %.4f контрактов по %.2f (%.2f USDC, комиссия %.4f)",
                request.participant, request.outcome, request.size, price,
                request.size * price, fee,
            )
            return OrderResult(
                status=OrderStatus.DRY_RUN,
                filled_size=request.size,
                avg_price=price,
                fee_usdc=fee,
                dry_run=True,
                raw={"note": "dry-run, ордер на биржу не отправлялся"},
            )

        client = self._client_for(request.participant)
        try:
            response = client.create_and_post_market_order(
                token_id=request.token_id,
                amount=request.size,
                price=price,
                side="BUY",
                order_type=settings.execution_taker_order_type,
            )
        except Exception as exc:  # noqa: BLE001 — любая ошибка биржи фиксируется
            logger.exception("ордер %s отклонён", request.participant)
            return OrderResult(
                status=OrderStatus.FAILED,
                error=f"{type(exc).__name__}: {str(exc)[:400]}",
            )

        return self._parse_response(response, request, price, schedule)

    @staticmethod
    def _parse_response(response, request: OrderRequest, price: float,
                        schedule: FeeSchedule) -> OrderResult:
        data = response if isinstance(response, dict) else getattr(response, "__dict__", {})
        success = data.get("success", True)
        order_id = data.get("orderID") or data.get("order_id") or data.get("id")

        def _f(*keys: str) -> float:
            for key in keys:
                value = data.get(key)
                if value not in (None, ""):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        filled = _f("takingAmount", "size_matched", "sizeMatched", "filled_size")
        avg = _f("price", "avg_price") or price

        if not success:
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=order_id,
                error=str(data.get("errorMsg") or data.get("error") or "отклонён биржей")[:400],
                raw=data,
            )
        if filled <= 0:
            return OrderResult(
                status=OrderStatus.REJECTED, order_id=order_id,
                error="нулевое исполнение", raw=data,
            )

        status = (
            OrderStatus.FILLED
            if filled >= request.size - 1e-6
            else OrderStatus.PARTIAL
        )
        return OrderResult(
            status=status,
            filled_size=round(filled, 6),
            avg_price=round(avg, 6),
            fee_usdc=taker_fee(filled, avg, schedule),
            order_id=str(order_id) if order_id else None,
            raw=data,
        )

    def balance_usdc(self, participant: str) -> float | None:
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            wallet = self._settings.wallet_for(participant)
            if wallet is None:
                return None
            client = self._client_for(participant)
            result = client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=wallet.signature_type,
                )
            )
            raw = getattr(result, "balance", None)
            if raw is None and isinstance(result, dict):
                raw = result.get("balance")
            return round(float(raw) / 1_000_000, 2) if raw is not None else None
        except Exception:  # noqa: BLE001 — баланс не критичен для работы
            logger.warning("не удалось прочитать баланс %s", participant)
            return None
