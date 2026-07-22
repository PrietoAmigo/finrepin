"""Purge the leftover ``source='sample'`` housing observations.

The dashboard used to seed clearly-labelled *placeholder* rows (``source =
'sample'``) for any indicator that had no live data yet, gated behind
``HOUSING_SEED_SAMPLE``. That feature was removed — the generator *and* the
``clear_sample_observations`` calls the live ingestors used to make when real
data superseded a placeholder — so any sample rows already written just linger
forever: nothing overwrites them (real data lands on different ``period`` dates)
and nothing deletes them. They keep showing up on the *Spain Housing* panels
with ``Source = sample``.

Delete them once, here, so a deploy clears the placeholder data. Real INE/MIVAU/
derived rows are untouched. No-op on a database that never seeded samples.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-22

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM region_observations WHERE source = 'sample'")


def downgrade() -> None:
    # Sample rows were placeholder data, never a source of truth; there is
    # nothing to restore. (The generator that made them no longer exists.)
    pass
