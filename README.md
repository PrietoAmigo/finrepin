# fintracker

A self-hosted, single-user financial tracker (this repository is `finrepin`;
the application/package is `fintracker`). It pulls **daily market data**
(stocks, crypto, forex) into Postgres, surfaces it in **Grafana**, pulls
**SEC XBRL fundamentals**, and sends a **weekly email** report. The whole
thing runs under Docker Compose and schedules itself — no external cron.

## What's implemented (M1–M6)

- **Scaffold** — uv project, multi-stage Dockerfile, Compose stack (`db`, `app`,
  `grafana`), env-based config, no secrets in source.
- **Schema + seed** — SQLAlchemy models + an Alembic initial migration; the six
  holdings plus BTC/ETH and EUR/USD are seeded into `instruments`.
- **Market ingestion** — daily OHLCV for equities, EUR/USD, and crypto
  (BTC-USD/ETH-USD) via yfinance, plus the live crypto spot from CoinGecko
  (no API key), upserted into `prices`. The first run **backfills the entire
  available history** per instrument; every run after that is **incremental**
  (it re-fetches only a few days back from the latest stored bar, so gaps
  self-heal).
- **SEC XBRL fundamentals** — resolves CIKs from `company_tickers.json`, detects
  new filings via the submissions feed (logged in `filings`), and upserts curated
  `us-gaap` (UNH/PRM) and `ifrs-full` (BN) facts into `fundamentals`. No documents
  are downloaded or stored. Requires `SEC_USER_AGENT` with a contact email.
- **Earnings dates** — next upcoming earnings date per equity via yfinance,
  stored in `earnings_dates` with an `is_estimated` flag; names with no coverage
  are skipped.
- **Weekly email** — HTML + plain-text report (current levels with weekly,
  monthly, and yearly moves, plus upcoming earnings) for the symbols listed
  in `REPORT_SYMBOLS` (all instruments when unset), sent via Gmail SMTP with
  STARTTLS. Skips gracefully if email isn't configured.
- **Self-scheduling** — APScheduler in the `app` container, timezone-aware
  (daily market/earnings/SEC checks, weekly email).
- **Grafana** — provisioned Postgres datasource + a starter *Market Overview*
  dashboard and a *Ticker Fundamentals* dashboard (pick one ticker; 10-year
  P/E, revenue, operating margin, P/FCF, total debt, MCap, and shares
  outstanding, with a combined
  dual-axis panel to compare any two). Backed by SQL views (migration 0002)
  that derive TTM series from the SEC facts. An *Add ticker* search box on
  the dashboard queues any new symbol: the app validates it against SEC
  EDGAR and Yahoo Finance and ingests its full price + fundamentals history
  within a minute or two (unknown tickers are marked not found).
- **Financial Statements dashboard** — pick one ticker and read its annual
  Income Statement, Balance Sheet, and Cash Flow as line-item × fiscal-year
  tables, with a multi-select fiscal-year filter. The three statements are
  collapsible rows that act as tabs; line items are clustered into the
  sections a filed statement uses (current vs non-current, operating /
  investing / financing, ...) with uppercase header rows. Backed by
  statement views (migration 0006) that map the curated SEC XBRL tags onto
  statement lines.

All core features are in. What remains is **M7/M8 polish**: richer
dashboards and deeper observability.

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
uv sync --frozen --extra dev
uv run pytest
```

CI (GitHub Actions) runs ruff, mypy, pytest, and an offline Alembic SQL
sanity check on every push and pull request.

## Quick start

```bash
cp .env.example .env
# edit .env: set strong POSTGRES_PASSWORD and GF_SECURITY_ADMIN_PASSWORD.
docker compose up -d --build
docker compose logs -f app        # watch the boot + first ingest
```

With `RUN_ON_START=true` (the default in the template) the app runs one market
ingest immediately, so data shows up without waiting for the scheduled time.

Then open Grafana at **http://localhost:3007** (user `admin`, password from
`GF_SECURITY_ADMIN_PASSWORD`) and look at the *Market Overview* dashboard.

Production (keep the DB port internal — don't load the dev override):

```bash
docker compose -f compose.yaml up -d --build
```

For hands-off updates on a server, see
[Continuous deployment (home server)](#continuous-deployment-home-server).

## Configuration

All configuration is environment-driven; see `.env.example` for the full list.
Key knobs: `TZ`, `DAILY_HOUR`/`DAILY_MINUTE` (daily ingest), `WEEKLY_*` (email),
`RUN_ON_START`, `LOG_LEVEL`.

### Security note

- Use a dedicated [Gmail app password](https://support.google.com/accounts/answer/185833)
  for `EMAIL_PASS`, never your account password. If a secret ever leaks
  (pasted into a chat, committed, logged), treat it as compromised and rotate
  it immediately.
- `.env` is git-ignored. Never commit real secrets.

## Continuous deployment (home server)

The deployment model is pull-based, so the server needs no inbound access
from the internet (and no reverse proxy or TLS — everything stays plain HTTP
on your LAN, with Grafana at `http://server:3007`):

1. On every push to `main`, CI runs the checks and — only if they pass —
   builds the image and publishes it as `ghcr.io/prietoamigo/finrepin:latest`
   (plus a per-commit `sha-...` tag for rollbacks).
2. On the server, a systemd timer (`finrepin-deploy.timer`, every 5 minutes)
   runs `deploy/deploy.sh`: it fast-forwards the checkout to `origin/main`,
   pulls newer images, and re-runs `docker compose up -d`. Compose recreates
   only containers whose image or configuration changed, so **everything**
   deploys itself — the app image, Grafana dashboards and provisioning,
   compose-file changes, and db/grafana image-tag bumps. Migrations run at
   app boot, so schema changes apply themselves too.

### One-time server setup (Debian 12)

Install Docker Engine + the Compose v2 plugin from Docker's official apt
repository ([instructions](https://docs.docker.com/engine/install/debian/)) —
Debian's own `docker.io`/`docker-compose` packages are too old for these
compose files. Then:

```bash
sudo git clone https://github.com/PrietoAmigo/finrepin.git /opt/finrepin
cd /opt/finrepin
sudo cp .env.example .env   # then set real passwords, email, SEC_USER_AGENT

# GHCR packages are private by default; use a GitHub PAT with read:packages.
# Log in as root — the deploy timer runs as root.
sudo docker login ghcr.io -u <github-username>

# Install the deploy timer; its first run brings the whole stack up.
sudo cp deploy/finrepin-deploy.service deploy/finrepin-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finrepin-deploy.timer
```

That's it — new pushes to `main` deploy themselves within ~5 minutes of CI
finishing. Deploy logs: `journalctl -u finrepin-deploy.service`.

### Notes

- The server checkout is a **deploy artifact, not a workspace**: `deploy.sh`
  hard-resets it to `origin/main`, discarding local edits to tracked files.
  Untracked files (`.env`, `backups/`) are never touched — configure the
  server through `.env`, everything else through git.
- The units assume the checkout lives at `/opt/finrepin`; adjust the paths in
  `deploy/finrepin-deploy.service` if yours differs. If the unit files
  themselves change in git, re-run the `cp`/`daemon-reload` step (rare).
- To roll back, revert the offending commit on `main` (the deploy follows) —
  or stop the timer (`sudo systemctl stop finrepin-deploy.timer`), pin
  `app`'s image to a `sha-...` tag in `compose.prod.yaml`, and `up -d`
  manually while you investigate.

## Backups

All state lives in the `pgdata` Docker volume. Dump the database on demand
(gzipped, into `./backups/`, which is git-ignored):

```bash
docker compose run --rm backup
```

Schedule that command from host cron if you want periodic backups.

## Layout

```
.
├── compose.yaml                # db + app + grafana (+ on-demand backup profile)
├── compose.override.yaml       # dev: expose Postgres to localhost
├── compose.prod.yaml           # CD: pull GHCR image + Watchtower auto-updates
├── Dockerfile                  # multi-stage, uv (locked deps), non-root, heartbeat healthcheck
├── alembic.ini
├── uv.lock                     # pinned dependency set (Docker + CI install from it)
├── migrations/                 # Alembic env + initial schema
├── grafana/                    # provisioned datasource + dashboards
├── src/fintracker/
│   ├── config.py               # env -> Settings
│   ├── db.py                   # engine, session, wait-for-db
│   ├── models.py               # SQLAlchemy schema
│   ├── seed.py                 # instruments registry (edit to change holdings)
│   ├── migrate.py              # Alembic upgrade at boot
│   ├── scheduler.py            # APScheduler jobs
│   ├── heartbeat.py / healthcheck.py
│   ├── run.py                  # entrypoint
│   ├── ingest/                 # prices, forex, crypto, market orchestrator,
│   │                           #   fundamentals + sec_client, earnings
│   └── report/                 # weekly email: data (queries), render (HTML/text), email_report (SMTP)
├── tests/                      # pure-parsing unit tests (no network/DB)
└── .github/workflows/ci.yml    # ruff + mypy + pytest + alembic offline check
```

## Local development without Docker

```bash
uv sync --frozen --extra dev
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
  covers the `.TO`, `.MC`, and `.V` tickers here. Full history is available
  (`period="max"`), which is what the initial backfill uses.
- **Crypto:** daily history via Yahoo (`BTC-USD`, `ETH-USD` — CoinGecko's
  keyless API caps history at 365 days), latest spot via CoinGecko's free
  `simple/price` endpoint (no key).
- **Fundamentals:** SEC `data.sec.gov` XBRL — numbers only, no documents;
  requires a descriptive `SEC_USER_AGENT` with a contact email.
