"""SQLAlchemy schema.

Kept in sync with the Alembic migrations under `migrations/versions`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Registry of everything we track (equities, indexes, crypto, forex)."""

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    # equity | index | crypto | forex | rate | onchain
    kind: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    # Per-source identifiers; null when a source doesn't cover the instrument.
    yahoo_symbol: Mapped[str | None] = mapped_column(String(32))
    coingecko_id: Mapped[str | None] = mapped_column(String(64))
    fred_series: Mapped[str | None] = mapped_column(String(32))
    ecb_series: Mapped[str | None] = mapped_column(String(64))
    coinmetrics_metric: Mapped[str | None] = mapped_column(String(48))
    # SEC: resolved lazily from company_tickers.json, then persisted.
    cik: Mapped[str | None] = mapped_column(String(10))
    taxonomy: Mapped[str | None] = mapped_column(String(16))  # us-gaap | ifrs-full


class Price(Base):
    """One daily bar per instrument. Crypto spot rows carry only `close`."""

    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint("instrument_id", "date", name="uq_prices_instrument_date"),
        Index("ix_prices_date", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[dt.date] = mapped_column(Date)
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32))


class Filing(Base):
    """SEC filings we have already seen, so each is processed exactly once."""

    __tablename__ = "filings"
    __table_args__ = (Index("ix_filings_filed_at", "filed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    accession_no: Mapped[str] = mapped_column(String(25), unique=True)
    form: Mapped[str] = mapped_column(String(16))
    filed_at: Mapped[dt.date] = mapped_column(Date)
    processed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Fundamental(Base):
    """Curated XBRL facts. Instant facts store period_start == period_end."""

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "taxonomy",
            "tag",
            "unit",
            "period_start",
            "period_end",
            name="uq_fundamentals_fact",
        ),
        Index("ix_fundamentals_instrument_tag", "instrument_id", "tag"),
        Index("ix_fundamentals_filed_at", "filed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    taxonomy: Mapped[str] = mapped_column(String(16))
    tag: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(28, 6))
    fiscal_year: Mapped[int | None]
    fiscal_period: Mapped[str | None] = mapped_column(String(4))
    form: Mapped[str | None] = mapped_column(String(16))
    accession_no: Mapped[str | None] = mapped_column(String(25))
    filed_at: Mapped[dt.date | None] = mapped_column(Date)


class TickerRequest(Base):
    """On-demand ticker requests queued from the Grafana search box.

    status: pending -> done | not_found | error. Rows are kept after
    processing so the dashboard's insert-on-refresh stays idempotent.
    """

    __tablename__ = "ticker_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    note: Mapped[str | None] = mapped_column(String(256))
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EarningsDate(Base):
    """Next upcoming earnings date per equity (one row per instrument)."""

    __tablename__ = "earnings_dates"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), unique=True
    )
    earnings_date: Mapped[dt.date] = mapped_column(Date)
    is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="yfinance")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
