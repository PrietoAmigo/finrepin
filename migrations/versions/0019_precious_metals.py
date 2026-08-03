"""Precious-metal instruments: a Stooq symbol per series.

Adds `instruments.stooq_symbol` so a `kind='metal'` instrument can name the
free, key-less Stooq daily CSV series it ingests from (`xauusd` for gold,
`xagusd` for silver — USD per troy ounce). Metal observations reuse the
`prices` table like every other daily bar; the seed registers the instruments
themselves, so no rows are inserted here.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-03

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("stooq_symbol", sa.String(length=24), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruments", "stooq_symbol")
