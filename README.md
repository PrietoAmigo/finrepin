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
2. On the server, [Watchtower](https://containrrr.dev/watchtower/) polls the
   registry every 5 minutes and restarts `app` on a new image. It is
   label-scoped: `db` and `grafana` are never auto-updated. Migrations run at
   boot, so schema changes apply themselves.

### One-time server setup (Debian 12)

Install Docker Engine + the Compose v2 plugin from Docker's official apt
repository ([instructions](https://docs.docker.com/engine/install/debian/)) —
Debian's own `docker.io`/`docker-compose` packages are too old for these
compose files. Then:

```bash
git clone https://github.com/PrietoAmigo/finrepin.git && cd finrepin
cp .env.example .env        # set real passwords, email, SEC_USER_AGENT

# GHCR packages are private by default; use a GitHub PAT with read:packages.
# (Also creates ~/.docker/config.json, which Watchtower mounts.)
docker login ghcr.io -u <github-username>

docker compose -f compose.yaml -f compose.prod.yaml up -d
```

That's it — new pushes to `main` deploy themselves within ~5 minutes of CI
finishing.

### Notes

- Only the **app image** auto-updates. Changes to the compose files, Grafana
  provisioning, or `.env` still need a `git pull` and a re-run of the
  `up -d` command above (rare).
- To roll back, pin `app`'s image to a `sha-...` tag in `compose.prod.yaml`
  and `up -d` again.
- Prefer not to run Watchtower? Delete its service from `compose.prod.yaml`
  and put the equivalent in cron instead:
  `docker compose -f compose.yaml -f compose.prod.yaml pull app && docker compose -f compose.yaml -f compose.prod.yaml up -d app`

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
  covers the `.TO`, `.MC`, and `.V` tickers here.
- **Crypto:** CoinGecko's free `simple/price` endpoint (no key).
- **Fundamentals:** SEC `data.sec.gov` XBRL — numbers only, no documents;
  requires a descriptive `SEC_USER_AGENT` with a contact email.
