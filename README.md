# fintracker

A self-hosted, single-user financial tracker. It pulls **daily market data**
(stocks, crypto, forex) into Postgres, surfaces it in **Grafana**, and (from a
later milestone) pulls **SEC XBRL fundamentals** and sends a **weekly email**.
The whole thing runs under Docker Compose and schedules itself — no external
cron.

This repository is the foundation described in `financial-tracker-plan.md`.
It implements the plan's build order through **milestone 3**.

## What's implemented (M1–M6)

- **Scaffold** — uv project, multi-stage Dockerfile, Compose stack (`db`, `app`,
  `grafana`), env-based config, no secrets in source.
- **Schema + seed** — SQLAlchemy models + an Alembic initial migration; the six
  holdings plus BTC/ETH and EUR/USD are seeded into `instruments`.
- **Market ingestion** — daily prices (yfinance), EUR/USD (yfinance), and crypto
  (CoinGecko, no API key) upserted into `prices`.
- **SEC XBRL fundamentals** — resolves CIKs from `company_tickers.json`, detects
  new filings via the submissions feed (logged in `filings`), and upserts curated
  `us-gaap` (UNH/PRM) and `ifrs-full` (BN) facts into `fundamentals`. No documents
  are downloaded or stored. Requires `SEC_USER_AGENT` with a contact email.
- **Earnings dates** — next upcoming earnings date per equity via yfinance,
  stored in `earnings_dates` with an `is_estimated` flag; names with no coverage
  are skipped.
- **Weekly email** — HTML + plain-text report (last-week price moves + current
  levels, upcoming earnings, new fundamentals from filings filed in the past
  week, and a link out to Grafana), sent via Gmail SMTP with STARTTLS. Skips
  gracefully if email isn't configured.
- **Self-scheduling** — APScheduler in the `app` container, timezone-aware
  (daily market/earnings/SEC checks, weekly email).
- **Grafana** — provisioned Postgres datasource + a starter *Market Overview*
  dashboard.

All of the plan's core features are in. What remains is **M7/M8 polish**:
richer dashboards and observability/retry hardening.

## Sending a test email now

```bash
docker compose exec app python -m fintracker.report.email_report
```

This builds the report from whatever is in the DB and sends it once (or logs a
skip if `EMAIL_USER`/`EMAIL_PASS`/`EMAIL_TO` aren't set). `sample_report.html`
in this repo is a static preview of the email design.

## Tests

Pure parsing logic (SEC fact extraction, submissions filtering) is covered in
`tests/` and runs without any network or database:

```bash
PYTHONPATH=src python tests/test_fundamentals.py
```

## Quick start

```bash
cp .env.example .env
# edit .env: set strong POSTGRES_PASSWORD and GF_SECURITY_ADMIN_PASSWORD.
docker compose up -d --build
docker compose logs -f app        # watch the boot + first ingest
```

With `RUN_ON_START=true` (the default in the template) the app runs one market
ingest immediately, so data shows up without waiting for the scheduled time.

Then open Grafana at **http://localhost:3000** (user `admin`, password from
`GF_SECURITY_ADMIN_PASSWORD`) and look at the *Market Overview* dashboard.

Production (keep the DB port internal — don't load the dev override):

```bash
docker compose -f compose.yaml up -d --build
```

## Configuration

All configuration is environment-driven; see `.env.example` for the full list.
Key knobs: `TZ`, `DAILY_HOUR`/`DAILY_MINUTE` (daily ingest), `WEEKLY_*` (email),
`RUN_ON_START`, `LOG_LEVEL`.

### Security note

- The Gmail app password and CoinMarketCap key shared earlier in plaintext are
  **compromised** — rotate the Gmail one before enabling email (M6); the CMC key
  is no longer used at all (crypto now comes from CoinGecko).
- `.env` is git-ignored. Never commit real secrets.

## Layout

```
.
├── compose.yaml                # db + app + grafana
├── compose.override.yaml       # dev: expose Postgres to localhost
├── Dockerfile                  # multi-stage, uv, non-root, heartbeat healthcheck
├── alembic.ini
├── migrations/                 # Alembic env + initial schema
├── grafana/                    # provisioned datasource + dashboards
└── src/fintracker/
    ├── config.py               # env -> Settings
    ├── db.py                    # engine, session, wait-for-db
    ├── models.py                # SQLAlchemy schema
    ├── seed.py                  # instruments registry
    ├── migrate.py               # Alembic upgrade at boot
    ├── scheduler.py             # APScheduler jobs
    ├── heartbeat.py / healthcheck.py
    ├── run.py                   # entrypoint
    ├── ingest/                  # prices, forex, crypto, market orchestrator,
    │                            #   fundamentals + sec_client, earnings
    └── report/                  # weekly email: data (queries), render (HTML/text), email_report (SMTP)
tests/                          # pure-parsing unit tests (no network/DB)
```

## Local development without Docker

```bash
uv venv && uv pip install -e ".[dev]"
# point at a local Postgres:
export DB_HOST=localhost DB_PORT=5432 DB_NAME=fintracker DB_USER=fintracker DB_PASSWORD=...
python -m fintracker.run          # migrates, seeds, ingests on start, schedules

# run a one-off ingest:
python -c "from fintracker.migrate import run_migrations; run_migrations()"
python -c "from fintracker.seed import seed_instruments; seed_instruments()"
python -c "from fintracker.ingest.market import ingest_market_data; ingest_market_data()"

# migrations via the CLI (URL is read from the same env vars):
alembic upgrade head
```

## Notes on data sources

- **Prices/forex:** Yahoo via `yfinance` — the only free source that reliably
  covers the `.TO`, `.MC`, and `.V` tickers here.
- **Crypto:** CoinGecko's free `simple/price` endpoint (no key).
- **Fundamentals (M4):** SEC `data.sec.gov` XBRL — numbers only, no documents;
  requires a descriptive `SEC_USER_AGENT` with a contact email.
