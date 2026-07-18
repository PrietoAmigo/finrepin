"""Interest-rate instruments: a FRED series id per rate.

Adds `instruments.fred_series` so `kind='rate'` instruments can name the
free, key-less FRED series they ingest from. Rate observations reuse the
`prices` table (the rate lands in `close`, like a forex/crypto spot row).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-18

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("fred_series", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruments", "fred_series")
