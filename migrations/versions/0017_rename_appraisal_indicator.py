"""Drop the obsolete ``appraisal_eur_m2`` housing indicator.

The MIVAU statistic is already the appraised (tasado) value, so a separate
"appraisal" series duplicated ``price_eur_m2``. The fourth price slot is now the
distinct protected-housing series ``price_eur_m2_protected`` (re-seeded from the
registry), so remove the old indicator and any observations it collected. No-op
on a database that never seeded it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-21

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM region_observations WHERE indicator = 'appraisal_eur_m2'")
    op.execute("DELETE FROM indicators WHERE code = 'appraisal_eur_m2'")


def downgrade() -> None:
    # The indicator was registry-managed; a downgrade re-deploys the old seed,
    # which recreates it on the next boot. Nothing to restore here.
    pass
