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

- **Spain housing (Grafana)** — a *Spain Housing* Grafana dashboard: a filled
  choropleth of Spain linked to time series, showing house **prices (€/m²)** and
  their **year-on-year % change** — plus **renta, población, densidad, viviendas,
  superficie, antigüedad** — by **region and timeframe**. Region data is stored
  at every granularity (nation → CCAA → province → municipality) with parent
  links, so any series rolls up. Click a region on the choropleth and every panel
  filters to it; cascading **CCAA → province → municipality** selectors keep the
  list short as you drill down. Prices come from the **Ministerio de Vivienda**;
  the demographic and income series from **INE**'s free Tempus3 JSON API. The
  choropleth is an Apache ECharts panel (the Business Charts plugin). See
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

A provisioned **Grafana** dashboard, *Spain Housing* (open Grafana at
**http://localhost:3007** and pick it from the dashboard list). It shows Spanish
house **prices (€/m²)** and their **year-on-year % change**, plus regional
**renta, población, densidad, viviendas, superficie and antigüedad**, by **region
and timeframe** — a filled choropleth of Spain wired to the time-series panels.

- **Everything in Grafana.** The choropleth is an **Apache ECharts** panel (the
  *Business Charts* plugin, `volkovlabs-echarts-panel`, installed on Grafana
  startup via `GF_INSTALL_PLUGINS`). It is **selection-only** — **click a region**
  to filter, there is no zoom or pan — and every panel (price, YoY, population,
  income, the all-indicators table) follows the selection. Template variables pick
  the map metric, the granularity (**CCAA / province / municipality**, province by
  default) and an extra series; **cascading CCAA → province → municipality**
  selectors narrow the region list as you drill down, so the municipality picker
  only ever lists the municipalities of the chosen province.
- **All granularities, stored.** The `regions` table holds the whole hierarchy —
  nation → CCAA → province → municipality (~8,200 regions) — with `parent_code`
  links, so a fine-grained series always rolls up to a coarser one. Observations
  land in one generic `region_observations` table; SQL views `v_region_series`
  (denormalised) and `v_region_yoy` (year-on-year %) are what the panels query.
- **Data sources.**
  - **Prices (€/m²)** — *Ministerio de Vivienda* (all / new / second-hand /
    protected-VPO). The ministry ships legacy `.XLS` **spreadsheets**, not a JSON
    API; each workbook has **one sheet per four-year block** and a **two-row
    header** (a year row over a quarter row). The ingest downloads them (built-in
    default URLs, override with `MIVAU_*_URL`), reads every sheet, reconstructs
    each column's `(year, quarter)` period from the two header rows, and maps each
    region row to every level it matches (see
    [Notes on data sources](#notes-on-data-sources)).
  - **Población and renta (per person/household)** — *INE*'s free, key-less
    Tempus3 JSON API (`DATOS_TABLA/<id>`). Población ships from table **2852**;
    municipal renta comes from the ADRH's "Indicadores de renta media y mediana"
    tables (operation **353**), one huge (~30k-series) table per province — so
    it is **not** auto-discovered (that would OOM the ingest); set
    `INE_RENTA_MUNI_TABLES` to the specific province table ids you want. Not
    every listed indicator is reachable
    this way: **dwelling counts** live in Tempus3 (table 3457) but aren't wired
    yet, while **mean floor area, mean dwelling age, and territory area (km²)**
    are only in INE's PC-Axis (`.px`) census tables — not this JSON API — so
    they (and the `densidad` derived from area) stay empty. No placeholder data
    is ever written.
  - Both run daily (`HOUSING_HOUR`/`HOUSING_MINUTE`) and once on boot. Trigger one
    by hand with `docker compose exec app python -m fintracker.housing.pipeline`.
    Panels stay empty for any indicator with no ingested rows yet — no placeholder
    data is ever written.
- **Geometry.** Province and CCAA polygons come from
  [es-atlas](https://github.com/martgnz/es-atlas) — feature ids are INE codes, so
  data joins to the map exactly — simplified and **inlined into the panel** (the
  plugin compiles its code synchronously, so the geometry can't be fetched at
  render time). The Canary Islands are drawn in the conventional inset below the
  peninsula.

> ⚠️ Built where the Spanish government APIs and Grafana itself were unreachable,
> so the **data path** (schema, seed, views, panel SQL) is verified against a real
> Postgres, but the **live ingests** (INE table specs, MIVAU spreadsheet layouts)
> and the **ECharts panel** could not be exercised end-to-end. They're written to
> the documented shapes and are easy to adjust (env-var table ids / URLs, the panel
> code) if the first real run needs a tweak. An earlier build also seeded
> clearly-labelled **placeholder rows** (`source = 'sample'`) for indicators with
> no live data; that feature was removed, and migration **0018** deletes any such
> rows left in an existing database, so panels now show real data or nothing.

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

## Manual operations

Two things you occasionally want to force by hand, on the server. They are
independent: updating the containers pulls new **code/images**; refreshing the
data re-runs the **ingests** against the running containers.

### Update the containers manually

The `finrepin-deploy.timer` does this every 5 minutes; to force it now, run the
deploy script (idempotent — the same steps the timer runs):

```bash
sudo /opt/finrepin/deploy/deploy.sh
```

Or the same steps spelled out (fast-forward the checkout, pull newer images,
recreate only what changed):

```bash
cd /opt/finrepin
sudo git fetch origin main && sudo git reset --hard origin/main
sudo docker compose -f compose.yaml -f compose.prod.yaml pull
sudo docker compose -f compose.yaml -f compose.prod.yaml up -d --remove-orphans
```

Grafana dashboard/provisioning changes arrive through the bind mounts and are
re-read within ~60s, so a dashboard-only change needs no image pull. For a local
(non-server) checkout that builds instead of pulling: `docker compose up -d --build`.

### Refresh the data manually

The ingests run on a daily schedule (and once on boot). To pull fresh data right
now, exec into the running `app` container:

```bash
# Spain housing — INE (población, renta, densidad, …) + Ministerio de Vivienda €/m²:
docker compose exec app python -m fintracker.housing.pipeline

# Market data — stocks, crypto, forex, interest rates:
docker compose exec app python -m fintracker.ingest.market
```

To refresh just one housing source (or re-seed the region/indicator reference
data):

```bash
docker compose exec app python -m fintracker.housing.ingest_ine     # INE series only
docker compose exec app python -m fintracker.housing.ingest_mivau   # €/m² prices (MIVAU) only
docker compose exec app python -m fintracker.housing.seed           # regions + indicators
```

(On the server these run against the container the deploy timer manages; prefix
with `sudo` if your Docker needs it — `exec` doesn't need the `-f compose.prod.yaml`
override.)

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
├── grafana/                    # provisioned datasource + dashboards + geo/ (Spain GeoJSON)
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
│   ├── housing/                # Spain housing: region hierarchy + indicators,
│   │                           #   INE + MIVAU ingest, data/regions_all.json
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
- **Spain regional series (INE):** INE's Tempus3 JSON API
  (`servicios.ine.es/wstempus/js/ES`) — free, no API key. Series are fetched by
  **`DATOS_TABLA/<table id>`** (fixed ids are reliable; operation-title discovery
  was too error-prone). **Población** works out of the box from table **2852**
  ("Población por provincias y sexo") — the sex total is kept and **CCAA +
  national are derived by summing provinces** (population is additive). **Renta**
  (Atlas de distribución de renta de los hogares) has several measures per table,
  so a label filter selects the intended one (`renta neta media por persona` /
  `… por hogar`). Municipal renta comes from the ADRH's "Indicadores de renta
  media y mediana" tables (operation `353`), but each is ONE PROVINCE and is
  huge (~30k series: municipality × district × section × six measures), so they
  are **not** auto-discovered — looping all 54 would pull ~1.6M series and OOM
  the ingest. Set `INE_RENTA_MUNI_TABLES` to the specific province table ids you
  want (district/section rows are dropped); the provincial/household aggregates
  take a pinned `INE_RENTA_PROV_TABLE`/`INE_RENTA_HOGAR_TABLE`. A compact
  province/CCAA renta source is still TODO. Only level series are stored (year-on-year % is derived
  by the `v_region_yoy` view). Ceuta/Melilla and small municipalities can be
  sparse in INE. **Not everything is in this JSON API:** dwelling **counts** are
  (table `3457`, not wired yet), but **mean floor area**, **mean dwelling age**
  (año de construcción) and **territory area (km²)** are published only as INE
  PC-Axis (`.px`) census tables, which `DATOS_TABLA` does not serve — so those,
  and the `densidad` that would derive from área, stay empty. No `source =
  'sample'` placeholder is ever written; migration 0018 removes any left by the
  old sample-data feature.
- **Market-activity series (INE + MIVAU):** alongside the €/m² prices, the
  registry also carries **home sales** (`compraventa`, INE ETDP, monthly/
  province, additive so it rolls up), the **House Price Index** (`ipv`, INE
  operation 25, quarterly/CCAA, an index) and **urban land price**
  (`precio_suelo_m2`, a MIVAU `.XLS`). These are **off by default** — pinned by
  id only, never auto-discovered — and stay empty until their env var is set
  (`INE_COMPRAVENTA_TABLE`, `INE_IPV_TABLE`, `MIVAU_SUELO_URL`; find the ids on
  the server, they can't be reached from CI). More adjacent series (mortgages,
  transactions, permits, Euríbor, affordability ratios) slot into the same
  generic store the same way.
- **Spain house prices (Ministerio de Vivienda):** the ministry (MIVAU/ex-Fomento)
  publishes its €/m² price statistics as legacy **`.XLS` spreadsheets** (the
  "BoletinOnline" sedal files: `35101000` all, `35101500` new, `35102000`
  second-hand, `35102500` protected/VPO), not a JSON API. Each workbook holds
  **one sheet per four-year block** ("Tabla 1" = 1995–1998, …) under a **two-row
  header** — a year row (`Año 1995`, once per four columns) above a quarter row
  (`1º 2º 3º 4º`). The ingest downloads each workbook (`.xls` via `xlrd`, with an
  HTML-table fallback), reads **every** sheet, carries each year across its four
  quarter columns to rebuild the period, and maps each region row into every
  level it matches — so one sheet fills nation, community and province. (A
  single-header fallback covers plain-year / `2024T1`-style layouts.) The default
  URLs are built in; override with `MIVAU_*_URL`. Note the
  whole statistic is the appraised (tasado) value, so there is no separate
  "appraisal" series — the fourth slot is protected housing (VPO) instead.
  **Catastro** was considered too but its free services are cadastral
  geometry/reference data (not transaction prices); the schema already stores all
  granularities (`regions.level` incl. `muni`) if you later add a cadastral or
  transaction-count series.
