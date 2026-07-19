"""Drop the BTC-RCAP instrument; the MVRV panel now derives realized cap.

Realized cap (Coin Metrics ``CapRealUSD``) turned out to need a paid key on the
free community API (403 Forbidden), so the seed now tracks the free MVRV ratio
(``CapMVRVCur``) as ``BTC-MVRV`` instead, and the panel/email derive realized
cap as ``market cap / MVRV``. Remove the now-unused ``BTC-RCAP`` instrument (its
price rows cascade; it never held any, since the ingest always 403'd) so the
ingest stops trying to fetch the forbidden metric. No-op on a fresh database.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-19

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM instruments WHERE symbol = 'BTC-RCAP'")


def downgrade() -> None:
    # BTC-RCAP was seed-managed; a downgrade deploys the old seed, which
    # recreates it on the next boot. Nothing to restore here.
    pass
