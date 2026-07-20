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
- **Yahoo fundamentals (non-SEC filers)** — listings that don't file with the
  SEC (CSU.TO, AI.PA, RMS.PA, KRI.AT, and any such on-demand ticker) get their
  annual + quarterly income statement, balance sheet, and cash flow from Yahoo
  Finance instead. Yahoo's line items are mapped onto the same canonical XBRL
  tags (stored under taxonomy `yahoo`), so the dashboards work identically —
  Yahoo just carries less history (roughly the last 4–5 fiscal years).
- **Earnings dates** — next upcoming earnings date per equity via yfinance,
  stored in `earnings_dates` with an `is_estimated` flag; names with no coverage
  are skipped.
- **Weekly email** — HTML + plain-text report (current levels with weekly,
  monthly, and yearly moves, plus upcoming earnings) for the symbols listed
  in `REPORT_SYMBOLS` (all instruments when unset), grouped into colour-coded
  Stocks / Crypto / Forex sections. The Crypto section is Bitcoin-focused: the
  BTC price and its MVRV Z-Score (shown as a unitless level with absolute
  moves), with ETH omitted from the email (still tracked on the dashboards).
  Sent via Gmail SMTP with STARTTLS; skips gracefully if email isn't configured.
- **Self-scheduling** — APScheduler in the `app` container, timezone-aware
  (daily market/earnings/SEC checks, weekly email).
- **Grafana** — provisioned Postgres datasource + a *Market Overview*
  dashboard (global and European index performance as % gain/loss of the
  daily close, benchmark interest rates — the most relevant one per region,
  from FRED — a BTC rainbow chart with the blockchaincenter.net color bands, a
  combined BTC price (USD) & MVRV Z-Score panel (BTC price on a log axis with
  the on-chain MVRV Z-Score on a second axis, its green/red bands marking the
  historical under/overvaluation extremes; it stands in for the plain BTC/USD
  close, whose line it already carries), and FX rates) and a *Ticker
  Fundamentals* dashboard (pick one ticker; price candlesticks with a daily/
  weekly/monthly candle selector plus SMA-50 and SMA-200 overlay lines that
  share the candle axis and whose latest and mean values read off a table
  legend, a dual-axis panel to compare any two metrics, and 10-year P/E, P/FCF,
  revenue, earnings, total debt, and shares outstanding — plus Price, P/B,
  EV/EBITDA, EPS, gross margin, operating margin, Debt-to-Equity, and MCap
  through the metric selectors). Backed by SQL views (migrations 0002–0012)
  that derive TTM series from the SEC facts. Global and European
  market indexes are seeded as `kind='index'` instruments and ingested from
  Yahoo like everything else. An *Add ticker* search box on the dashboard
  queues any new symbol: the app validates it against SEC EDGAR and Yahoo
  Finance and ingests its full price + fundamentals history within a minute
  or two (unknown tickers are marked not found). Grafana boots straight into
  Market Overview (no welcome/news home page), and the time picker offers
  quick ranges up to *Last 15 years*.
- **Currency switching** — the per-ticker dashboards have a *Currency* selector listing
  all currencies seen on tracked tickers (listing + reporting currencies).
  Money values — prices, revenue, debt, MCap, statement lines — are converted
  into the selected currency at the matching day's FX rate (fiscal-year-end
  rate for statement tables); ratios and share counts are left alone. Backed
  by the `fx_usd_daily` view (migration 0007): the forex ingest auto-registers
  a `<CCY>/USD` pair per currency it encounters, and rates gap-fill across
  weekends. If a rate series hasn't been ingested yet, values fall back to
  unconverted rather than disappearing.
- **Financial Statements dashboard** — pick one ticker and read its annual
  Income Statement, Balance Sheet, and Cash Flow as line-item × fiscal-year
  tables, with a multi-select fiscal-year filter. The three statements are
  collapsible rows that act as tabs; line items are clustered into the
  sections a filed statement uses (current vs non-current, operating /
  investing / financing, ...) with uppercase header rows. Backed by
  statement views (migration 0006) that map the curated SEC XBRL tags onto
  statement lines.

- **Spain housing dashboard** — an interactive choropleth of Spain linked to a
  time series, served by a small FastAPI app (the `housing` service) at
  **http://localhost:3008**. House prices come from **INE**'s free, key-less
  Tempus3 JSON API — the **House Price Index** (Índice de Precios de Vivienda,
  IPV), a quarterly index (base 2015=100) by autonomous community with a
  national total, in three components (overall / new-build / resale). Click a
  region on the map and the time series filters to it (overlaid on the national
  line); scrub the quarter slider and the map recolours to that quarter. An
  indicator selector, a *Index level / Year-on-year %* metric toggle (the map
  switches from a sequential to a diverging red/blue scale), light/dark themes,
  and shareable deep-link URLs (`#region=ccaa-13&metric=yoy`) round it out. It
  reuses the app image and reads the same Postgres; the INE ingest is scheduled
  daily and runs on boot. Until the first successful ingest — or with no
  database at all — the page renders clearly-labelled **sample** data so it's
  explorable out of the box, then switches to live data automatically. See
  [Spain housing dashboard](#spain-housing-dashboard) below.

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

## Spain housing dashboard

An interactive map of Spanish house prices linked to a time series, at
**http://localhost:3008** (the `housing` service). Unlike the Grafana
dashboards it is a small custom single-page app (Apache ECharts) with the one
interaction Grafana can't do cleanly: **click a region on the map and the time
series filters to it.**

- **Data — INE, free and key-less.** The House Price Index (Índice de Precios de
  Vivienda, IPV) from INE's Tempus3 JSON API: a quarterly index (base 2015=100)
  by autonomous community with a national total, in three components (overall,
  new-build, resale). No API key is required. The ingest discovers the
  by-community IPV table from the IPV operation at run time (so it self-heals if
  INE renumbers a table); pin it with `INE_IPV_TABLE` if you ever need to. It's
  state-aware like the market ingestors — full history on the first run,
  incremental afterwards — scheduled daily (`HOUSING_HOUR`/`HOUSING_MINUTE`) and
  run once on boot. Trigger one by hand with
  `docker compose exec app python -m fintracker.housing.ingest`.
- **Interactions.** Click a community to filter the time series (drawn over the
  national line for comparison); scrub the quarter slider to recolour the map
  through time; switch indicator (overall / new / resale); toggle *Index level*
  vs *Year-on-year %* (the map switches from a sequential blue scale to a
  diverging red/blue one centred on 0). Light/dark aware. Selection, indicator,
  and metric live in the URL hash, so a view like
  `http://localhost:3008/#region=ccaa-13&metric=yoy` is shareable.
- **Sample-data fallback.** With no housing rows yet — or no database — the app
  serves clearly-labelled *sample* data (a plausible boom→bust→recovery shape,
  **not** real figures) so the page is explorable immediately; a banner says so,
  and the first successful INE ingest switches it to live data automatically.
- **Geography.** Community and province polygons come from
  [es-atlas](https://github.com/martgnz/es-atlas) (INE codes as feature ids, so
  the data joins to the map exactly); the Canary Islands are drawn in the
  conventional inset below the peninsula. Province geometry ships too — the
  schema and map are province-ready for a future province-level indicator (e.g.
  the Ministerio de Vivienda €/m² series or INE property-transaction counts).

The dashboard is not part of the Grafana stack and needs no login. It reads the
same Postgres, so it deploys and updates through the same Compose flow as
everything else.

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
├── compose.yaml                # db + app + grafana + housing (+ on-demand backup profile)
├── compose.override.yaml       # dev: expose Postgres to localhost
├── compose.prod.yaml           # CD: pull GHCR image + Watchtower auto-updates
├── Dockerfile                  # multi-stage, uv (locked deps), non-root, heartbeat healthcheck
├── alembic.ini
├── uv.lock                     # pinned dependency set (Docker + CI install from it)
├── migrations/                 # Alembic env + initial schema
├── grafana/                    # provisioned datasource + dashboards
├── web/                        # housing dashboard SPA (ECharts) + vendored geo/echarts, sample data
├── src/fintracker/
│   ├── config.py               # env -> Settings
│   ├── db.py                   # engine, session, wait-for-db
│   ├── models.py               # SQLAlchemy schema
│   ├── seed.py                 # instruments registry (edit to change holdings)
│   ├── migrate.py              # Alembic upgrade at boot
│   ├── scheduler.py            # APScheduler jobs
│   ├── heartbeat.py / healthcheck.py
│   ├── run.py                  # entrypoint
│   ├── webapp.py               # FastAPI app for the Spain housing dashboard
│   ├── ingest/                 # prices, forex, crypto, market orchestrator,
│   │                           #   fundamentals + sec_client, earnings
│   ├── housing/                # Spain housing: INE ingest, region registry, dataset shaping, sample
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
- **Bitcoin on-chain (MVRV Z-Score):** Bitcoin market cap (`CapMrktCurUSD`) and
  the MVRV ratio (`CapMVRVCur`) from the **Coin Metrics Community API** (free, no
  key), stored as `kind='onchain'` instruments whose daily value lands in
  `close` (like a rate). Realized cap itself (`CapRealUSD`) needs a paid key on
  that API, but `CapMVRVCur = market cap / realized cap` is free, so realized cap
  is recovered exactly as `market cap / MVRV`. The MVRV Z-Score is not stored —
  the Market Overview panel derives it in SQL as `(market cap − market cap /
  MVRV) / stddev(market cap)` over the full stored history, so it self-calibrates
  as history grows (the same approach the rainbow chart takes with its
  regression). Full history backfills on the first run, incremental thereafter —
  the same state-aware path as prices.
- **Interest rates:** one benchmark per region, from free, key-less endpoints.
  Most come from FRED (Federal Reserve Economic Data) via its `fredgraph.csv`
  download: US 10-year Treasury (`DGS10`, daily), Japan and Australia 10-year
  government bond yields (OECD `IRLTLT01…` monthly series), Brazil's government
  T-bill rate as a SELIC proxy (`INTGSTBRM193N`, monthly), and the ICE BofA
  emerging-markets USD-bond index yield (`BAMLEMCBPIEY`, daily). The euro-area
  benchmark comes from the **ECB Data Portal** instead — its daily 10-year
  all-issuer government bond spot rate (`YC.B.U2.EUR.4F.G_N_C.SV_C_YM.SR_10Y`)
  — because FRED's monthly OECD euro-area series lags by months. Both sources
  need no API key. Full history backfills on the first run, incremental
  thereafter — the same state-aware path as prices.
- **Fundamentals:** SEC `data.sec.gov` XBRL — numbers only, no documents;
  requires a descriptive `SEC_USER_AGENT` with a contact email. Names without
  SEC coverage fall back to Yahoo Finance statements via `yfinance` (~4–5
  fiscal years of history, in the company's reporting currency — which can
  differ from the listing currency, e.g. CSU.TO trades in CAD but reports in
  USD, so its P/E mixes the two).
- **Spain house prices:** INE's Tempus3 JSON API
  (`servicios.ine.es/wstempus/js/ES`) — free, no API key. The House Price Index
  (IPV) is fetched via `DATOS_TABLA/<id>` for the by-community table, which the
  ingest finds via `TABLAS_OPERACION/IPV` at run time (override with
  `INE_IPV_TABLE`). Only the index series are stored; year-on-year change is
  derived on the dashboard. Ceuta and Melilla can be sparse in INE's series and
  simply show no data where absent. The Ministerio de Vivienda (€/m² by
  province) and Catastro were considered too: the ministry publishes
  spreadsheets rather than a JSON API and Catastro's free services are cadastral
  geometry/reference data (not transaction prices), so they're left as
  province-level extension points — the schema (`level='prov'`) and the shipped
  province geometry are ready for them.
