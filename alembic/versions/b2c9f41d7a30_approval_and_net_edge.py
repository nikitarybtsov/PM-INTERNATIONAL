"""Одобрение оператора, edge после комиссии и ссылка на реальный ордер.

Revision ID: b2c9f41d7a30
Revises: e8d4a5693d5f
Create Date: 2026-08-12

Проект перешёл на реальные деньги: заявка исполняется только после одобрения
человеком, а edge считается с учётом комиссии тейкера.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c9f41d7a30"
down_revision = "e8d4a5693d5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("decisions") as batch:
        batch.add_column(sa.Column("net_edge", sa.Float(), nullable=True))
        batch.add_column(sa.Column("taker_fee_usdc", sa.Float(), nullable=True))
        batch.add_column(sa.Column("approved_by", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("live_order_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("decisions") as batch:
        batch.drop_column("live_order_id")
        batch.drop_column("approved_at")
        batch.drop_column("approved_by")
        batch.drop_column("taker_fee_usdc")
        batch.drop_column("net_edge")
