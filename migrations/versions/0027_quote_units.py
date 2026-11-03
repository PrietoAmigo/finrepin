"""Minor-unit quotes: store pence/cents/agorot listings in the major unit.

Yahoo quotes London in pence (GBp), Johannesburg in cents (ZAc) and Tel Aviv
in agorot (ILA). Those prices were stored as quoted, and the code upper-cased
to GBP / ZAC / ILA, so every price-based ratio of such a listing was 100x too
high, and ZAC, which has no FX pair, converted at 1 to anything.

- instruments.price_divisor: what Yahoo's quotes are divided by before they
  are stored (100 for a minor-unit listing, else 1). NULL marks an equity
  whose Yahoo quote unit hasn't been checked yet: the daily market ingest
  (fintracker.ingest.quote_units) asks Yahoo once per such equity, divides its
  stored history and its trades into the major unit where needed, and records
  the divisor. That repair needs Yahoo (GBp was stored as GBP, so the data
  alone can't tell pence from pounds), which is why it isn't done here.
- Nothing but equities is quoted in a minor unit, so every other kind gets 1.
- The FX pairs registered for the pseudo-codes (ZAC/USD and the like) have no
  Yahoo quote and never had prices; they are deleted.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("price_divisor", sa.Integer(), nullable=True))
    op.execute("UPDATE instruments SET price_divisor = 1 WHERE kind <> 'equity'")
    op.execute(
        "DELETE FROM instruments WHERE kind = 'forex' "
        "AND split_part(symbol, '/', 1) IN ('GBX', 'ZAC', 'ILA')"
    )


def downgrade() -> None:
    op.drop_column("instruments", "price_divisor")
