"""Единый промпт для всех LLM-участников.

Codex и Claude получают ПОЛНОСТЬЮ идентичные системный и пользовательский
промпты, идентичный snapshot и идентичные ограничения. Единственное отличие —
сведения о собственном банке. Это гарантирует честность сравнения.
"""

from __future__ import annotations

import json

from app.adapters.participants.base import PortfolioView
from app.config import PROMPT_VERSION, get_settings
from app.schemas.decision import json_schema_for_prompt
from app.schemas.snapshot import MarketSnapshot

SYSTEM_PROMPT = """\
Ты — участник публичного эксперимента по прогнозированию матчей Dota 2 на \
рынках The International (Polymarket). Ты управляешь банком в USDC.

Правила:
1. Решение принимается ТОЛЬКО на основании переданного snapshot. Другой информации нет.
2. Ты не видишь решения других участников и не должен их предполагать.
3. Ты обязан вернуть РОВНО один JSON-объект по заданной схеме, без пояснений вокруг.
4. estimated_probability — твоя оценка вероятности исхода YES (метка YES указана в snapshot).
5. Ставь только там, где видишь преимущество (edge) над рыночной ценой. HOLD — нормальный ответ.
5а. Рынок выбираешь сам. В snapshot есть основной рынок и список other_markets:
    победители отдельных карт, тоталы, форы, экзотика. Ставить можно на любой —
    укажи его market_id в target_market_id. Недооценённым чаще оказывается не
    исход серии, а второстепенный рынок с тонким стаканом: там меньше внимания
    и цена дольше остаётся кривой. Проверяй ликвидность: на неликвидном рынке
    проскальзывание съест преимущество.
6. Соблюдай лимиты риска, они одинаковы для всех участников и указаны в запросе.
7. max_acceptable_price — максимальная цена, по которой ты согласен купить (0..1).
8. Пиши short_reason на русском языке, кратко и по делу.

КОМИССИЯ. Вход всегда идёт тейкером — выкупом стакана, поэтому комиссия платится \
всегда: fee = размер_в_контрактах × {fee_rate} × p × (1 − p), где p — цена входа. \
В долях от вложенной суммы это {fee_rate} × (1 − p): при цене 0.70 — около 1.5%, \
при 0.50 — около 2.5%.

Из этого следует главное: покупка по цене p оправдана только если твоя оценка \
вероятности q ≥ p + {fee_rate}·p·(1 − p). Ставка с преимуществом в полпроцента \
убыточна после комиссии. Сравнивай свою оценку именно с этим порогом, а не с \
голой ценой, и не выдавай за преимущество то, что съест биржа.

НЕСОСТОЯВШИЕСЯ КАРТЫ. Рынки на конкретную карту (Game 2, Game 3 и далее) \
существуют до того, как известно, состоится ли эта карта вообще. Если серия \
заканчивается раньше — например, Bo3 при счёте 2:0 — карты не будет, и \
Polymarket гасит ОБЕ стороны по 0.50, а не возвращает деньги по цене покупки.

Считай это третьим исходом, а не отменой сделки:
  * контракт, купленный дороже 0.50, теряет разницу: вход по 0.72 вернёт 0.50;
  * контракт дешевле 0.50 приносит прибыль: вход по 0.24 вернёт те же 0.50.

Поэтому оценивай так: P(карта состоится) × P(исход | карта состоялась) плюс \
P(карты не будет) × 0.50. На Bo3 вероятность отсутствия третьей карты — это \
вероятность счёта 2:0 в любую сторону, и у явного фаворита она велика. \
Дорогие контракты на третью карту при сильном фаворите в серии почти всегда \
проигрышны именно по этой причине, даже когда прогноз на сам исход верен.
"""

USER_PROMPT_TEMPLATE = """\
ФАЗА РАУНДА: {phase}
{phase_hint}

SNAPSHOT РЫНКА (единый для всех участников):
{snapshot_json}

ТВОЙ БАНК (данные только по тебе):
{portfolio_json}

ЛИМИТЫ РИСКА (одинаковые для всех, заявка сверх лимита будет урезана или отклонена):
{limits_json}

ФОРМАТ ОТВЕТА — верни ровно один JSON-объект:
{schema_json}

Требования к полям:
- action: BUY_YES (купить YES), BUY_NO (купить NO), SELL (закрыть/сократить открытую позицию), HOLD (пропустить).
- Для HOLD: stake_usdc = 0, max_acceptable_price можно не указывать (null).
- Для BUY_*: stake_usdc > 0 и max_acceptable_price обязателен.
- SELL допустим только если у тебя есть открытая позиция по этому рынку.
Ответ — только JSON.
"""

_PHASE_HINTS = {
    "PREMATCH": (
        "Матч ещё не начался, драфт не проводился. Оцениваешь серию по составам, "
        "форме и истории встреч."
    ),
    "AFTER_DRAFT": (
        "Драфт завершён, пики героев известны, карта ещё не началась. Это самый "
        "информативный момент: состав героев часто говорит о шансах больше, чем "
        "рейтинги команд. Если оператор передал пики в контексте — опирайся прежде "
        "всего на них."
    ),
    "BETWEEN_MAPS": (
        "Идёт перерыв между картами, оператор подтвердил окончание предыдущей карты. "
        "Учитывай счёт серии: он меняет и вероятности, и мотивацию. "
        "Ставки внутри идущей карты запрещены."
    ),
}


def build_prompt(snapshot: MarketSnapshot, portfolio: PortfolioView) -> tuple[str, str]:
    """Вернуть (system_prompt, user_prompt). Идентичен для Codex и Claude."""
    risk = get_settings().risk
    limits = {
        "max_position_pct_of_bank": risk.max_position_pct,
        "max_per_match_pct_of_bank": risk.max_match_pct,
        "max_total_open_exposure_pct": risk.max_total_exposure_pct,
        "min_stake_usdc": risk.min_stake_usdc,
        "min_market_liquidity_usdc": risk.min_liquidity_usdc,
        "max_slippage_bps": risk.max_slippage_bps,
        "max_stake_now_usdc": round(
            min(
                portfolio.available_balance,
                portfolio.cash_balance * risk.max_position_pct,
            ),
            2,
        ),
    }
    portfolio_json = json.dumps(
        {
            "participant": portfolio.participant_key,
            "initial_balance_usdc": portfolio.initial_balance,
            "cash_balance_usdc": portfolio.cash_balance,
            "reserved_usdc": portfolio.reserved_balance,
            "available_usdc": portfolio.available_balance,
            "open_exposure_usdc": portfolio.open_exposure_usdc,
            "exposure_on_this_market_usdc": portfolio.market_exposure_usdc,
        },
        ensure_ascii=False,
        indent=2,
    )
    user = USER_PROMPT_TEMPLATE.format(
        phase=snapshot.phase.value,
        phase_hint=_PHASE_HINTS.get(snapshot.phase.value, ""),
        snapshot_json=json.dumps(snapshot.to_prompt_dict(), ensure_ascii=False, indent=2),
        portfolio_json=portfolio_json,
        limits_json=json.dumps(limits, ensure_ascii=False, indent=2),
        schema_json=json_schema_for_prompt(),
    )
    return system_prompt(), user


def system_prompt() -> str:
    """Системный промпт со ставкой комиссии, подставленной из конфигурации.

    Ставка одинакова для обоих участников — иначе условия эксперимента разошлись бы.
    """
    from app.config import get_settings

    return SYSTEM_PROMPT.format(fee_rate=get_settings().polymarket_taker_fee_rate)


def prompt_version() -> str:
    return PROMPT_VERSION
