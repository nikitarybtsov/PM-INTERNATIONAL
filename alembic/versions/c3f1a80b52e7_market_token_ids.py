"""Токены исходов рынка: без них нельзя отправить ордер на биржу.

Polymarket торгует не «рынком», а конкретным токеном YES или NO. До этой
миграции токенов не было нигде, и боевое исполнение упиралось в их отсутствие.

Revision ID: c3f1a80b52e7
Revises: b2c9f41d7a30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3f1a80b52e7"
down_revision = "b2c9f41d7a30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("markets", sa.Column("yes_token_id", sa.String(128), nullable=True))
    op.add_column("markets", sa.Column("no_token_id", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("markets", "no_token_id")
    op.drop_column("markets", "yes_token_id")
