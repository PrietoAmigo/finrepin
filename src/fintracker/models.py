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
    open: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    high: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    low: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(30, 8))
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


class Region(Base):
    """A Spanish territory at some granularity, in a nation→CCAA→province→
    municipality hierarchy. ``parent_code`` links each region to the next level
    up, so any indicator stored at a fine level can be rolled up (or a coarse
    level read directly). ``ine_code`` is the official INE code within the level
    (2 digits for CCAA/province, 5 for municipality); ``lat``/``lon`` is a
    representative centroid (handy for point maps and labels). Seeded from the
    map geometry so every region has a matching polygon.
    """

    __tablename__ = "regions"
    __table_args__ = (
        Index("ix_regions_level", "level"),
        Index("ix_regions_parent", "parent_code"),
    )

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    level: Mapped[str] = mapped_column(String(8))  # nation | ccaa | prov | muni
    ine_code: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(160))
    parent_code: Mapped[str | None] = mapped_column(String(16))
    lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    lon: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))


class Indicator(Base):
    """Registry of the region time series we track (prices, income, population,
    housing stock, ...), independent of which regions carry them. Drives both
    the ingest (what to fetch) and the dashboard (what to offer)."""

    __tablename__ = "indicators"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    # eur_m2 | eur | count | m2 | km2 | year | inhab_km2
    unit: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(16))  # MIVAU | INE
    frequency: Mapped[str] = mapped_column(String(2))  # A | Q | M
    # price | income | demographic | housing | area
    category: Mapped[str] = mapped_column(String(24))
    higher_is: Mapped[str] = mapped_column(String(8), default="neutral")


class RegionObservation(Base):
    """One value of an indicator for a region at a period — the generic time
    series store. ``period`` is normalised to the first day of its period
    (quarter/year/month) so series from different regions and sources align on a
    shared timeline.
    """

    __tablename__ = "region_observations"
    __table_args__ = (
        UniqueConstraint(
            "region_code", "indicator", "period", name="uq_region_obs_region_indicator_period"
        ),
        Index("ix_region_obs_indicator_period", "indicator", "period"),
        Index("ix_region_obs_region_indicator", "region_code", "indicator"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region_code: Mapped[str] = mapped_column(String(16))
    indicator: Mapped[str] = mapped_column(String(40))
    period: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    source: Mapped[str] = mapped_column(String(16))
