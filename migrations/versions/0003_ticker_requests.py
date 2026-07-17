"""Ticker requests queue for the dashboard's add-a-ticker search box.

The Grafana panel INSERTs into this table (via a data-modifying CTE);
a minutely scheduler job processes pending rows.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-17

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticker_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False, unique=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("note", sa.String(length=256), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ticker_requests_status", "ticker_requests", ["status"])


def downgrade() -> None:
    op.drop_table("ticker_requests")
