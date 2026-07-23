"""Regional series from INE's free, key-less Tempus3 JSON API.

INE (Instituto Nacional de Estadística) serves every table as JSON at
``…/DATOS_TABLA/<id>`` and lists an operation's tables at
``…/TABLAS_OPERACION/<op>``. This module is a small **spec-driven engine**: each
``IneSpec`` names an indicator, the operation + title keywords used to discover
its table, the geographic level, the frequency, and the label filters that pick
the intended measure when a table carries several. The engine fetches, resolves
each series' region (by name for CCAA/province, by INE code for municipalities),
and upserts the values.

⚠️ The table-selection keywords and value filters are best-effort: they are
written against INE's documented JSON shape but were **not** validated against
live responses (the environment that built this could not reach INE). Adjust a
spec's ``keywords``/``value_filters`` — or pin a table id with the matching
``*_TABLE`` env var — if an indicator comes back empty on the first real run.

Run one off-schedule ingest by hand with:
    python -m fintracker.housing.ingest_ine
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import requests
from sqlalchemy import delete, func, select, text
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.housing.regions import (
    normalize,
    region_code_from_ine_code,
    region_code_from_ine_name,
)
from fintracker.housing.store import upsert_observations
from fintracker.models import RegionObservation

log = logging.getLogger(__name__)

SOURCE = "INE"
_TIMEOUT = (10, 60)
# Skip variation/rate series; we store levels and derive change in SQL.
_SKIP_TOKENS = ("variacion", "tasa de", "porcentaje")
_MUNI_CODE_RE = re.compile(r"\b(\d{5})\b")


@dataclass(frozen=True)
class IneSpec:
    indicator: str
    level: str  # ccaa | prov | muni
    frequency: str  # A | Q | M
    default_table: str = ""  # table id fetched when no env override is set
    table_env: str = ""  # env var pinning a single table id
    tables_env: str = ""  # env var pinning a comma-separated list of table ids
    value_filters: tuple[str, ...] = ()  # normalized substrings a series MUST contain
    exclude_values: tuple[str, ...] = ()  # normalized substrings that DROP a series
    operation: str = ""  # INE operation code, for title discovery (fallback only)
    keywords: tuple[str, ...] = ()  # title substrings for discovery
    all_tables: bool = False  # discovery: loop EVERY matching table
    exclude: tuple[str, ...] = ("grupo",)  # discovery: title substrings that disqualify


# Series aggregated up the hierarchy (province → CCAA → nation) after ingest,
# because they are additive counts.
_SUMMABLE = ("poblacion", "compraventa", "hipoteca", "viviendas_total", "viviendas_principales")

# Prefer known DATOS_TABLA ids over operation-title discovery (INE operation
# codes are easy to get wrong; a fixed table id is reliable).
INE_SPECS: list[IneSpec] = [
    # Population by province — INE table 2852 ("Población por provincias y sexo").
    # CCAA + national are derived by summing provinces (population is additive),
    # so no separate table is needed for them.
    IneSpec("poblacion", "prov", "A", default_table="2852",
            table_env="INE_POBLACION_PROV_TABLE", exclude_values=("hombres", "mujeres")),
    # Renta at CCAA level from the ECV compact tables (9947 renta por persona,
    # 9949 por hogar) — territory by name, "renta neta media por persona/hogar"
    # picks the measure (the "por unidad de consumo" / "con alquiler imputado"
    # variants don't match). Province/municipal renta from the ADRH is too large
    # to fetch (see the muni spec), so renta shows at CCAA granularity.
    IneSpec("renta_persona", "ccaa", "A", default_table="9947",
            table_env="INE_RENTA_PROV_TABLE",
            value_filters=("renta neta media por persona",)),
    IneSpec("renta_hogar", "ccaa", "A", default_table="9949",
            table_env="INE_RENTA_HOGAR_TABLE",
            value_filters=("renta neta media por hogar",)),
    # Municipal renta lives in the ADRH's "Indicadores de renta media y mediana"
    # tables (operation 353), one per province. Each is HUGE — ~30k series
    # (municipality × district × section × six measures), so auto-discovering and
    # fetching all 54 would pull ~1.6M series and OOM the ingest, and the section
    # rows carry 5-digit codes that collide with municipality codes. So this is
    # env-gated: set INE_RENTA_MUNI_TABLES to specific province table ids to
    # enable it. exclude_values drops the district/section series, keeping only
    # municipality-level rows. (A compact province/CCAA renta source is TODO.)
    IneSpec("renta_persona", "muni", "A", tables_env="INE_RENTA_MUNI_TABLES",
            value_filters=("renta neta media por persona",),
            exclude_values=("seccion", "distrito")),
    # --- Market activity (small province/CCAA tables; pinned ids only, never
    # auto-discovered, so no OOM risk). ---------------------------------------
    # Home sales — Estadística de Transmisión de Derechos de la Propiedad, table
    # 6149: national+CCAA+province × título × Número. At province level the
    # "compraventa" título is kept; additive → rolls up to CCAA/nation.
    IneSpec("compraventa", "prov", "M", default_table="6149",
            table_env="INE_COMPRAVENTA_TABLE",
            value_filters=("compraventa",)),
    # House Price Index (IPV) — INE operation 15, table 80270 ("Índices por CCAA:
    # general, vivienda nueva y de segunda mano. Trimestrales"). Quarterly,
    # national + CCAA. Keep the general INDEX (drop the variación rows); an index,
    # so not additive.
    IneSpec("ipv", "ccaa", "Q", default_table="80270",
            table_env="INE_IPV_TABLE",
            value_filters=("general",),
            exclude_values=("variacion", "tasa")),
    # Mortgages — Estadística de Hipotecas, table 76317 (nacional + provincias).
    # Keep the total-fincas mortgage COUNT on the current "base nueva" series
    # (the table also carries the old/linked bases); province, additive.
    IneSpec("hipoteca", "prov", "M", default_table="76317",
            table_env="INE_HIPOTECA_TABLE",
            value_filters=("numero de hipotecas", "total fincas", "base nueva"),
            exclude_values=("importe",)),
    # Dwelling counts — INE table 3457 "Viviendas según tamaño del municipio por
    # tipo de vivienda" (nacional + CCAA + provincias). In this JSON view the
    # municipality-size dimension is collapsed to its all-sizes total, labelled
    # "Total habitantes", so each series is one (territory × tipo-de-vivienda).
    # Pin the tipo — "Total viviendas" for the grand total, "Vivienda principal"
    # (singular) for the main-residence stock — plus "total habitantes" so we
    # stay on the all-sizes rollup even if INE later expands the size bands.
    # Province, additive → rolls up to CCAA/nation. (Census 2011.)
    IneSpec("viviendas_total", "prov", "A", default_table="3457",
            table_env="INE_VIVIENDAS_TABLE",
            value_filters=("total viviendas", "total habitantes")),
    IneSpec("viviendas_principales", "prov", "A", default_table="3457",
            table_env="INE_VIVIENDAS_TABLE",
            value_filters=("vivienda principal", "total habitantes"),
            exclude_values=("no principal",)),
]


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def fetch_json(path: str, params: dict[str, Any] | None = None) -> Any:
    base = get_settings().ine_base_url.rstrip("/")
    resp = requests.get(f"{base}/{path.lstrip('/')}", params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:
        # INE answers 200 with an empty/HTML body for an unknown table or
        # operation id; surface that cleanly instead of a raw JSONDecodeError.
        snippet = " ".join(resp.text[:160].split())
        raise ValueError(
            f"INE returned non-JSON for {path} ({len(resp.text)} bytes): {snippet!r}"
        ) from exc


def choose_tables(tables: list[dict[str, Any]], spec: IneSpec) -> list[str]:
    """All table ids whose title matches every keyword and no exclusion. Pure."""
    out: list[str] = []
    for table in tables:
        name = normalize(str(table.get("Nombre", "")))
        table_id = table.get("Id")
        if table_id is None:
            continue
        if any(x in name for x in spec.exclude):
            continue
        if all(kw in name for kw in spec.keywords):
            out.append(str(table_id))
    return out


def choose_table(tables: list[dict[str, Any]], spec: IneSpec) -> str | None:
    """The first table id matching the spec (single-table discovery). Pure."""
    matches = choose_tables(tables, spec)
    return matches[0] if matches else None


def _labels(series: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for meta in series.get("MetaData") or []:
        name = meta.get("Nombre")
        if isinstance(name, str) and name.strip():
            labels.append(name.strip())
    name = series.get("Nombre")
    if isinstance(name, str):
        labels.append(name)
        labels.extend(seg.strip() for seg in name.split(".") if seg.strip())
    return labels


def series_region(series: dict[str, Any], level: str) -> str | None:
    """Resolve a series' region code at ``level`` from its labels. Pure."""
    labels = _labels(series)
    if level == "muni":
        for label in labels:
            match = _MUNI_CODE_RE.search(label)
            if match:
                code = region_code_from_ine_code(match.group(1), "muni")
                if code:
                    return code
        return None
    for label in labels:
        code = region_code_from_ine_name(label, level)
        if code:
            return code
    return None


def series_matches(series: dict[str, Any], spec: IneSpec) -> bool:
    """Whether a series is the measure we want (filters pass, not a variation)."""
    joined = normalize(" ".join(_labels(series)))
    if any(tok in joined for tok in _SKIP_TOKENS):
        return False
    if any(tok in joined for tok in spec.exclude_values):
        return False
    return all(f in joined for f in spec.value_filters)


def _period(day: dt.date, frequency: str) -> dt.date:
    if frequency == "A":
        return dt.date(day.year, 1, 1)
    if frequency == "Q":
        return dt.date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
    return dt.date(day.year, day.month, 1)


def rows_from_series(series: dict[str, Any], frequency: str) -> list[tuple[dt.date, float]]:
    """Parse a series' Data into (period, value), oldest first. Pure."""
    out: dict[dt.date, float] = {}
    for point in series.get("Data") or []:
        fecha, valor = point.get("Fecha"), point.get("Valor")
        if fecha is None or valor is None:
            continue
        try:
            day = dt.datetime.fromtimestamp(int(fecha) / 1000, tz=dt.UTC).date()
            out[_period(day, frequency)] = float(valor)
        except (ValueError, TypeError, OverflowError):
            continue
    return sorted(out.items())


def parse_table(
    series_list: list[dict[str, Any]], spec: IneSpec
) -> list[tuple[str, dt.date, float]]:
    """Flatten a DATOS_TABLA response into (region_code, period, value) rows. Pure."""
    rows: list[tuple[str, dt.date, float]] = []
    for series in series_list:
        if not series_matches(series, spec):
            continue
        region = series_region(series, spec.level)
        if region is None:
            continue
        for period, value in rows_from_series(series, spec.frequency):
            rows.append((region, period, value))
    return rows


def _resolve_table_ids(spec: IneSpec) -> list[str]:
    """Table ids to ingest: pinned env, then default id, then title discovery."""
    if spec.tables_env:
        pinned = os.environ.get(spec.tables_env, "").strip()
        if pinned:
            return [t.strip() for t in pinned.split(",") if t.strip()]
    if spec.table_env:
        pinned = os.environ.get(spec.table_env, "").strip()
        if pinned:
            return [pinned]
    if spec.default_table:
        return [spec.default_table]
    env_hint = spec.tables_env or spec.table_env or "a table env"
    if not spec.operation:
        log.info("INE %s: no table id configured — set %s to enable.", spec.indicator, env_hint)
        return []
    try:
        raw = fetch_json(f"TABLAS_OPERACION/{spec.operation}")
    except Exception as exc:  # unknown operation, non-JSON, network — best-effort
        log.warning("INE discovery failed for %s (op %s): %s", spec.indicator, spec.operation, exc)
        return []
    tables = raw if isinstance(raw, list) else []
    ids = choose_tables(tables, spec) if spec.all_tables else (
        [t] if (t := choose_table(tables, spec)) else []
    )
    if not ids:
        log.warning("No INE table matched %s (keywords=%s); set %s.",
                    spec.indicator, spec.keywords, env_hint)
    return ids


_LEVEL_PREFIX = {"nation": "es", "ccaa": "ccaa-", "prov": "prov-", "muni": "muni-"}


def _has_rows(indicator: str, level: str) -> bool:
    """Whether live rows already exist for this indicator AT this level.

    Per-level so the first run of a level (e.g. municipal renta after provincial)
    still backfills its full history rather than only the last few periods.
    """
    prefix = _LEVEL_PREFIX.get(level, "")
    with session_scope() as session:
        return (
            session.execute(
                select(func.count())
                .select_from(RegionObservation)
                .where(
                    RegionObservation.indicator == indicator,
                    RegionObservation.source == SOURCE,
                    RegionObservation.region_code.like(f"{prefix}%"),
                )
            ).scalar_one()
            > 0
        )


def _ingest_table(table_id: str, spec: IneSpec, params: dict[str, Any]) -> int:
    try:
        series_list = fetch_json(f"DATOS_TABLA/{table_id}", params=params)
    except Exception:
        log.exception("INE DATOS_TABLA fetch failed for %s (table %s)", spec.indicator, table_id)
        return 0
    if not isinstance(series_list, list):
        return 0
    parsed = parse_table(series_list, spec)
    if not parsed:
        log.warning(
            "Parsed 0 rows for %s from INE table %s (%d series)",
            spec.indicator, table_id, len(series_list),
        )
        return 0
    rows = [(region, spec.indicator, period, value) for region, period, value in parsed]
    return upsert_observations(rows, SOURCE)


def ingest_spec(spec: IneSpec) -> int:
    table_ids = _resolve_table_ids(spec)
    if not table_ids:
        return 0
    params: dict[str, Any] = {"det": 2}
    if _has_rows(spec.indicator, spec.level):
        params["nult"] = 6
    written = sum(_ingest_table(tid, spec, params) for tid in table_ids)
    if written:
        log.info(
            "Ingested %d INE rows for %s (level %s, %d table(s))",
            written, spec.indicator, spec.level, len(table_ids),
        )
    return written


_DERIVE_DENSITY = text(
    """
    INSERT INTO region_observations (region_code, indicator, period, value, source)
    SELECT p.region_code, 'densidad', p.period, p.value / a.value, 'derived'
    FROM region_observations p
    JOIN (
        SELECT DISTINCT ON (region_code) region_code, value
        FROM region_observations
        WHERE indicator = 'superficie_km2' AND value > 0
        ORDER BY region_code, period DESC
    ) a ON a.region_code = p.region_code
    WHERE p.indicator = 'poblacion'
    ON CONFLICT (region_code, indicator, period)
      DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source
    """
)


def derive_density() -> int:
    """densidad = poblacion / superficie_km2, per region and population period.

    Uses each region's latest area (area is a single static reference row from
    ``territory.py``, so it applies to every population year). Recomputed from
    scratch each run (stale derived rows are dropped first) so revisions in
    either input propagate.
    """
    with session_scope() as session:
        session.execute(
            delete(RegionObservation).where(
                RegionObservation.indicator == "densidad",
                RegionObservation.source == "derived",
            )
        )
        result = session.execute(_DERIVE_DENSITY)
    return int(getattr(result, "rowcount", 0) or 0)


# Roll an additive indicator up the hierarchy: parent = SUM(children). Run for
# child='prov' then 'ccaa' in one transaction so CCAA totals feed the national
# total. Only applied to indicators ingested at the finest (province) level.
_AGGREGATE_UP = text(
    """
    INSERT INTO region_observations (region_code, indicator, period, value, source)
    SELECT r.parent_code, o.indicator, o.period, SUM(o.value), 'derived'
    FROM region_observations o
    JOIN regions r ON r.code = o.region_code
    WHERE o.indicator = :ind AND r.level = :child AND r.parent_code IS NOT NULL
    GROUP BY r.parent_code, o.indicator, o.period
    ON CONFLICT (region_code, indicator, period)
      DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source
    """
)


def derive_aggregates(indicator: str) -> int:
    """Sum an additive indicator province → CCAA → nation (live rows only)."""
    written = 0
    with session_scope() as session:
        for child in ("prov", "ccaa"):
            result = session.execute(_AGGREGATE_UP, {"ind": indicator, "child": child})
            written += int(getattr(result, "rowcount", 0) or 0)
    return written


def ingest_ine() -> int:
    """Run every INE spec and derive aggregates from what was ingested."""
    total = 0
    touched: set[str] = set()
    for spec in INE_SPECS:
        written = ingest_spec(spec)
        if written:
            touched.add(spec.indicator)
        total += written
    for indicator in _SUMMABLE:
        if indicator in touched:
            try:
                derive_aggregates(indicator)
            except Exception:
                log.exception("Aggregation failed for %s", indicator)
    try:
        derive_density()
    except Exception:
        log.exception("Density derivation failed")
    return total


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_ine()
