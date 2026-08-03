"""Drop `instruments.stooq_symbol`; metals come from Yahoo futures.

Stooq's free CSV download now sits behind a JavaScript browser-verification
wall — every request returns HTTP 200 with an HTML challenge page, on both
`stooq.com` and the `stooq.pl` mirror — so the metals ingest reads `GC=F`/`SI=F`
from Yahoo like every other daily bar and nothing carries a Stooq symbol any
more. The stored `prices` rows are untouched; only the unused column goes.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-03

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("instruments", "stooq_symbol")


def downgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("stooq_symbol", sa.String(length=24), nullable=True),
    )
