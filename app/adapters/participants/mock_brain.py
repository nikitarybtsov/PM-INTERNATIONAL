"""Детерминированный «мозг» для mock-режима участников.

Нужен, чтобы весь проект работал без единого API-ключа: демо повторяемо,
но участники дают заметно разные решения (у каждого свой стиль).

Стили:
  codex — консервативный, требует большего edge, ставит меньше;
  claude — умеренный, ставит при среднем edge;
  titan-bot — не используется (Титан вводит решения вручную), оставлен
              как запасной вариант для авто-демо.
"""

from __future__ import annotations

import hashlib

from app.adapters.participants.base import PortfolioView
from app.config import get_settings
from app.constants import Action
from app.schemas.decision import TradeDecisionInput
from app.schemas.snapshot import MarketSnapshot

_STYLES: dict[str, dict[str, float]] = {
    "codex": {"bias": -0.035, "edge_threshold": 0.05, "kelly_fraction": 0.35, "confidence": 0.58},
    "claude": {"bias": 0.030, "edge_threshold": 0.035, "kelly_fraction": 0.50, "confidence": 0.66},
    "titan": {"bias": 0.055, "edge_threshold": 0.03, "kelly_fraction": 0.65, "confidence": 0.72},
}


def _unit(key: str) -> float:
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:6], "big") / float(1 << 48)


def mock_decision(
    participant_key: str, snapshot: MarketSnapshot, portfolio: PortfolioView
) -> TradeDecisionInput:
    style = _STYLES.get(participant_key, _STYLES["claude"])
    risk = get_settings().risk

    seed_key = f"{participant_key}:{snapshot.snapshot_id}:{snapshot.market.external_id}"
    noise = (_unit(seed_key) - 0.5) * 0.09  # ±4.5 п.п.

    market_yes = snapshot.yes_price
    estimate_yes = min(0.97, max(0.03, market_yes + style["bias"] + noise))
    edge_yes = estimate_yes - market_yes
    edge_no = (1 - estimate_yes) - snapshot.no_price

    if edge_yes >= edge_no:
        action, outcome, edge = Action.BUY_YES, "YES", edge_yes
    else:
        action, outcome, edge = Action.BUY_NO, "NO", edge_no

    ask = snapshot.best_ask_for(outcome)
    depth = snapshot.book_for(outcome).ask_depth_usdc

    if (
        edge < style["edge_threshold"]
        or ask is None
        or depth < risk.min_liquidity_usdc
        or portfolio.available_balance < risk.min_stake_usdc
    ):
        return TradeDecisionInput(
            action=Action.HOLD,
            estimated_probability=round(estimate_yes, 4),
            stake_usdc=0.0,
            max_acceptable_price=None,
            confidence=round(style["confidence"] * 0.8, 3),
            short_reason=(
                f"Оценка {estimate_yes:.2f} против рынка {market_yes:.2f}: "
                f"преимущество {edge:+.3f} ниже порога {style['edge_threshold']:.3f}. Пропускаю."
            ),
            key_factors=[f"рыночная цена YES {market_yes:.2f}", f"ликвидность {depth:.0f} USDC"],
            risk_factors=["недостаточный edge", "риск переплаты по спреду"],
            information_used=["snapshot цены и стакан", "состояние собственного банка"],
        )

    # доля Келли от банка, урезанная стилем и лимитом позиции
    kelly = max(0.0, edge / max(1e-6, 1 - ask)) * style["kelly_fraction"]
    stake = min(
        portfolio.cash_balance * min(kelly, risk.max_position_pct),
        portfolio.available_balance,
    )
    stake = round(max(stake, 0.0), 2)
    if stake < risk.min_stake_usdc:
        stake = round(min(risk.min_stake_usdc, portfolio.available_balance), 2)

    max_price = round(min(0.99, ask + 0.02), 4)
    label = snapshot.market.yes_label if outcome == "YES" else snapshot.market.no_label

    return TradeDecisionInput(
        action=action,
        estimated_probability=round(estimate_yes, 4),
        stake_usdc=stake,
        max_acceptable_price=max_price,
        confidence=round(min(0.95, style["confidence"] + edge), 3),
        short_reason=(
            f"Оцениваю победу «{label}» выше рынка: моя вероятность "
            f"{(estimate_yes if outcome == 'YES' else 1 - estimate_yes):.2f} против "
            f"цены {ask:.2f}, edge {edge:+.3f}."
        ),
        key_factors=[
            f"edge {edge:+.3f} по исходу {outcome}",
            f"лучший ask {ask:.2f}",
            f"глубина стакана {depth:.0f} USDC",
        ],
        risk_factors=[
            "выборка матчей мала, оценка формы шумная",
            "возможны замены в составах",
            "проскальзывание при тонком стакане",
        ],
        information_used=[
            "snapshot: цены YES/NO",
            "snapshot: стакан и ликвидность",
            "snapshot: контекст оператора" if snapshot.operator_context else "snapshot: без заметок",
        ],
    )
