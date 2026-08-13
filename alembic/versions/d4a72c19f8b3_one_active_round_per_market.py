"""Один живой раунд на рынок — ограничение на уровне БД.

Проверка в коде не ловит гонку: два одновременных запроса читают базу до
записи друг друга и оба видят «свободно». Так появились раунды #9 и #10 с
разницей в 1.8 секунды, после чего решения первого сгорели как устаревшие.

Частичный уникальный индекс делает это невозможным. Синтаксис у SQLite и
PostgreSQL здесь совпадает, поэтому используется прямой SQL: alembic не умеет
задавать условие индекса переносимо.

Revision ID: d4a72c19f8b3
Revises: c3f1a80b52e7
"""

from __future__ import annotations

from alembic import op

revision = "d4a72c19f8b3"
down_revision = "c3f1a80b52e7"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('OPEN', 'LOCKED', 'AWAITING_APPROVAL')"


def upgrade() -> None:
    # Существующие дубли закрываем, иначе индекс не создастся. Оставляем
    # самый свежий раунд по каждому рынку — у него актуальный снимок.
    op.execute(
        f"""
        UPDATE rounds
           SET status = 'CANCELLED',
               note = COALESCE(note, '') || ' [дубль закрыт миграцией]'
         WHERE {ACTIVE}
           AND id NOT IN (
               SELECT MAX(id) FROM rounds WHERE {ACTIVE} GROUP BY market_id
           )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX ux_active_round_per_market "
        f"ON rounds (market_id) WHERE {ACTIVE}"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_active_round_per_market")
