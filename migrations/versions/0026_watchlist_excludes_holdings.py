"""The watchlist is for what you don't own: holdings can't be on it.

The weekly email now always lists your holdings in a section of their own, so
the watchlist is reserved for stocks, crypto, and metals you *don't* hold. This
is enforced in the database, so the Manage dashboard's forms, the CLI, and
ad-hoc SQL all follow the same rule:

- portfolio_held_instruments — instruments with an open position: some account
                               has bought more units than it has sold.
- instruments_watchlist_guard (BEFORE INSERT / UPDATE OF in_watchlist ON
  instruments) — trying to watch a holding leaves it unwatched, without an
  error, so a multi-symbol "Add to watchlist" still adds the rest.
- portfolio_unwatch_holdings (AFTER any write ON portfolio_transactions,
  per statement) — a buy that opens a position takes the instrument off the
  watchlist. Selling out doesn't put it back; re-add it if you want to.

Anything already both held and watched is taken off the watchlist here. The
email still shows it, under Holdings.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Per account, so a data-entry over-sell in one account can't hide a real
# position in another. Mirrors fintracker.portfolio.held_symbols.
PORTFOLIO_HELD_INSTRUMENTS = """
CREATE VIEW portfolio_held_instruments AS
SELECT DISTINCT s.instrument_id
FROM (
    SELECT t.account_id, t.instrument_id
    FROM portfolio_transactions t
    GROUP BY t.account_id, t.instrument_id
    HAVING sum(CASE WHEN t.side = 'buy' THEN t.quantity ELSE -t.quantity END) > 0
) s
"""

UNWATCH_HOLDINGS = """
UPDATE instruments SET in_watchlist = false
WHERE in_watchlist
  AND id IN (SELECT h.instrument_id FROM portfolio_held_instruments h)
"""

INSTRUMENTS_WATCHLIST_GUARD = """
CREATE FUNCTION instruments_watchlist_guard() RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NEW.in_watchlist AND EXISTS (
        SELECT 1 FROM portfolio_held_instruments h WHERE h.instrument_id = NEW.id
    ) THEN
        NEW.in_watchlist := false;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER instruments_watchlist_guard
BEFORE INSERT OR UPDATE OF in_watchlist ON instruments
FOR EACH ROW EXECUTE FUNCTION instruments_watchlist_guard();
"""

PORTFOLIO_UNWATCH_HOLDINGS = f"""
CREATE FUNCTION portfolio_unwatch_holdings() RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    {UNWATCH_HOLDINGS.strip()};
    RETURN NULL;
END
$fn$;

CREATE TRIGGER portfolio_unwatch_holdings
AFTER INSERT OR UPDATE OR DELETE ON portfolio_transactions
FOR EACH STATEMENT EXECUTE FUNCTION portfolio_unwatch_holdings();
"""


def upgrade() -> None:
    op.execute(PORTFOLIO_HELD_INSTRUMENTS)
    op.execute(UNWATCH_HOLDINGS)
    op.execute(INSTRUMENTS_WATCHLIST_GUARD)
    op.execute(PORTFOLIO_UNWATCH_HOLDINGS)


def downgrade() -> None:
    op.execute("DROP TRIGGER portfolio_unwatch_holdings ON portfolio_transactions")
    op.execute("DROP FUNCTION portfolio_unwatch_holdings()")
    op.execute("DROP TRIGGER instruments_watchlist_guard ON instruments")
    op.execute("DROP FUNCTION instruments_watchlist_guard()")
    op.execute("DROP VIEW portfolio_held_instruments")
