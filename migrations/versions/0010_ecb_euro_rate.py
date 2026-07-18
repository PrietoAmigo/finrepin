"""ECB-sourced rates: an ECB Data Portal series key per rate.

Adds `instruments.ecb_series` so a `kind='rate'` instrument can be fed from the
ECB Data Portal instead of FRED. The euro-area benchmark moves here: FRED's
monthly OECD euro-area series lags by months, whereas the ECB publishes a daily
euro-area yield-curve spot rate. The stale FRED-sourced EU10Y rows are dropped
so the ECB ingest owns that instrument cleanly.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-19

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("ecb_series", sa.String(length=64), nullable=True),
    )
    # Drop the lagging monthly euro-area rows imported from FRED; the ECB ingest
    # backfills the daily history in their place. No-op on a fresh database.
    op.execute(
        "DELETE FROM prices WHERE source = 'fred' AND instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol = 'EU10Y')"
    )


def downgrade() -> None:
    op.drop_column("instruments", "ecb_series")
