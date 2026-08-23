"""Watchlist flag on instruments, editable from the Manage dashboard.

Adds ``instruments.in_watchlist`` and seeds it from the ``REPORT_SYMBOLS`` env
var (when set) so the weekly-email watchlist the user already configured shows
up pre-populated in the UI. When nothing is flagged the report falls back to
REPORT_SYMBOLS and then to every instrument, so behaviour is unchanged until the
watchlist is edited.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-20

"""
from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column(
            "in_watchlist",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Data seed needs a live connection; skip it when generating offline SQL.
    if context.is_offline_mode():
        return

    raw = os.environ.get("REPORT_SYMBOLS", "").strip()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if symbols:
        op.get_bind().execute(
            sa.text(
                "UPDATE instruments SET in_watchlist = true "
                "WHERE upper(symbol) = ANY(:symbols)"
            ),
            {"symbols": symbols},
        )


def downgrade() -> None:
    op.drop_column("instruments", "in_watchlist")
