"""CLI: инициализация БД, seed, демо-раунд.

Запуск:
    python -m app.cli init          # создать таблицы и участников
    python -m app.cli seed          # + загрузить рынки из активного провайдера
    python -m app.cli demo          # полный демо-сценарий: раунд, решения, расчёт
    python -m app.cli export        # сохранить комплект файлов в EXPORT_DIR
    python -m app.cli status        # краткий scoreboard в терминале
"""

from __future__ import annotations

import argparse
import json
import sys

from app.config import LIVE_TRADING_ENABLED, get_settings
from app.constants import Phase
from app.db.base import create_all, session_scope
from app.schemas.decision import TradeDecisionInput
from app.services import export as export_service
from app.services import rounds as rounds_service
from app.services import seed as seed_service
from app.services import settlement as settlement_service
from app.services import stats as stats_service

DEMO_TITAN_DECISION = TradeDecisionInput(
    action="BUY_YES",
    estimated_probability=0.68,
    stake_usdc=75,
    max_acceptable_price=0.95,
    confidence=0.8,
    short_reason="По опыту игры на ранге Титан фаворит выглядит недооценённым рынком.",
    key_factors=["форма команды на последних турнирах", "удобный патч под их стиль"],
    risk_factors=["возможна замена игрока", "тонкий стакан на верхних уровнях"],
    information_used=["snapshot: цены и стакан", "личный игровой опыт"],
)


def cmd_init(_: argparse.Namespace) -> int:
    create_all()
    with session_scope() as db:
        participants = seed_service.seed_participants(db)
    print(f"БД готова. Участники: {', '.join(p.key for p in participants)}")
    print(f"Стартовый банк каждого: {get_settings().initial_bankroll_usdc:.2f} USDC")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    create_all()
    with session_scope() as db:
        result = seed_service.seed_all(db, with_markets=True)
    print(f"Участники: {', '.join(result['participants'])}")
    print(f"Загружено рынков: {len(result['markets'])}")
    for market in result["markets"]:
        print(f"  #{market['id']}  {market['title']}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Полный демо-раунд без единого API-ключа."""
    create_all()
    with session_scope() as db:
        result = seed_service.seed_all(db, with_markets=True)
        if not result["markets"]:
            print("Нет доступных рынков", file=sys.stderr)
            return 1
        from app.db.models import Market

        market = db.get(Market, result["markets"][0]["id"])

        print(f"1. Рынок: {market.title}")
        round_row = rounds_service.create_round(
            db, market, Phase.PREMATCH, operator_context="Демо-раунд: составы без изменений"
        )
        print(f"2. Snapshot #{round_row.snapshot_id} зафиксирован, раунд #{round_row.id} открыт")

        rounds_service.request_ai_decisions(db, round_row)
        print("3. Codex и Claude ответили (mock-режим), решения скрыты")

        rounds_service.submit_manual_decision(db, round_row, "titan", DEMO_TITAN_DECISION)
        print(f"4. Titan подал решение, раунд заперт: {round_row.status}")

        report = rounds_service.execute_round(db, round_row)
        print("5. Risk engine + paper execution:")
        for key, entry in report.items():
            print(
                f"   {key:<7} {entry['verdict']:<9} "
                f"{entry['requested_stake']:.2f} → {entry['approved_stake']:.2f} USDC"
                + (
                    f", исполнено {entry.get('filled_size', 0):.2f} @ "
                    f"{entry.get('avg_price', 0):.4f}"
                    if entry["executed"]
                    else ", сделки не было"
                )
            )

        rounds_service.reveal_round(db, round_row)
        print("6. Решения раскрыты")

        if args.settle:
            settlement_service.settle_market(db, market, args.settle)
            print(f"7. Рынок рассчитан: победил {args.settle}")

        board = stats_service.scoreboard(db)
        print("\nScoreboard:")
        for p in board["participants"]:
            print(
                f"  {p['participant']:<7} банк {p['equity']:>9.2f} $  "
                f"PnL {p['pnl_abs']:>+8.2f} ({p['pnl_pct']:>+6.2f}%)  "
                f"Brier {p['forecast']['brier'] if p['forecast']['brier'] is not None else '—'}"
            )
        print(f"\n🏆 Лучший трейдер: {board['winners']['best_trader']['participant']}")
        print(f"🎯 Лучший прогнозист: {board['winners']['best_forecaster']['participant'] or '—'}")
        print(f"\nОткройте http://localhost:8000/ui/rounds/{round_row.id}")
    return 0


def cmd_export(_: argparse.Namespace) -> int:
    with session_scope() as db:
        files = export_service.write_all(db)
    print("Файлы сохранены (публикация не выполняется):")
    for kind, path in files.items():
        print(f"  {kind:<10} {path}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    with session_scope() as db:
        board = stats_service.scoreboard(db)
    print(json.dumps(board["winners"], ensure_ascii=False, indent=2))
    for p in board["participants"]:
        print(
            f"{p['participant']:<7} equity {p['equity']:>9.2f}  "
            f"PnL {p['pnl_abs']:>+8.2f}  ставок {p['bets_count']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pm-international",
        description="Титан vs Codex vs Claude — paper trading (реальных сделок нет)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="создать таблицы и участников").set_defaults(func=cmd_init)
    sub.add_parser("seed", help="загрузить рынки из активного провайдера").set_defaults(
        func=cmd_seed
    )
    demo = sub.add_parser("demo", help="прогнать полный демо-раунд")
    demo.add_argument(
        "--settle",
        choices=["YES", "NO"],
        default="YES",
        help="результат рынка для расчёта (по умолчанию YES)",
    )
    demo.set_defaults(func=cmd_demo)
    sub.add_parser("export", help="сохранить экспорт в EXPORT_DIR").set_defaults(func=cmd_export)
    sub.add_parser("status", help="краткий scoreboard").set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if LIVE_TRADING_ENABLED:  # pragma: no cover — константа всегда False
        print("LIVE TRADING включён — выполнение остановлено", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
