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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.housing.regions import (
    normalize,
    region_code_from_ine_code,
    region_code_from_ine_name,
)
from fintracker.housing.seed import clear_sample_observations
from fintracker.models import RegionObservation

log = logging.getLogger(__name__)

SOURCE = "INE"
_TIMEOUT = (10, 60)
_UPSERT_CHUNK = 500
# Skip variation/rate series; we store levels and derive change in SQL.
_SKIP_TOKENS = ("variacion", "tasa de", "porcentaje")
_MUNI_CODE_RE = re.compile(r"\b(\d{5})\b")


@dataclass(frozen=True)
class IneSpec:
    indicator: str
    operation: str  # INE operation code (e.g. "EPOB", "ADRH")
    keywords: tuple[str, ...]  # normalized substrings the table title must contain
    level: str  # ccaa | prov | muni
    frequency: str  # A | Q | M
    value_filters: tuple[str, ...] = ()  # normalized substrings a series must contain
    table_env: str = ""  # env var pinning a single table id
    tables_env: str = ""  # env var pinning a comma-separated list of table ids
    all_tables: bool = False  # loop EVERY table whose title matches (e.g. one per province)
    exclude: tuple[str, ...] = ("grupo",)  # title substrings that disqualify a table


# Best-effort specs. Operation codes: EPOB = Estadística del Padrón Continuo,
# ADRH = Atlas de distribución de renta de los hogares. The ADRH renta table
# carries several measures (neta/bruta, per person/household, medians), so
# ``value_filters`` selects the intended one; municipal renta is published one
# table per province, so those specs loop every matching table (``all_tables``,
# overridable with a comma-separated ``*_TABLES`` env var).
INE_SPECS: list[IneSpec] = [
    IneSpec("poblacion", "EPOB", ("poblacion", "comunidad"), "ccaa", "A",
            ("total",), table_env="INE_POBLACION_CCAA_TABLE"),
    IneSpec("poblacion", "EPOB", ("poblacion", "provincia"), "prov", "A",
            ("total",), table_env="INE_POBLACION_PROV_TABLE"),
    IneSpec("renta_persona", "ADRH", ("renta", "media", "persona"), "prov", "A",
            ("renta neta media por persona",), table_env="INE_RENTA_PROV_TABLE"),
    IneSpec("renta_hogar", "ADRH", ("renta", "media", "hogar"), "prov", "A",
            ("renta neta media por hogar",), table_env="INE_RENTA_HOGAR_TABLE"),
    IneSpec("renta_persona", "ADRH", ("renta", "municipios"), "muni", "A",
            ("renta neta media por persona",), tables_env="INE_RENTA_MUNI_TABLES",
            all_tables=True, exclude=("distrito", "seccion", "grupo")),
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
    return resp.json()


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
    """Table ids to ingest: pinned env (plural or single) overrides discovery."""
    if spec.tables_env:
        pinned = os.environ.get(spec.tables_env, "").strip()
        if pinned:
            return [t.strip() for t in pinned.split(",") if t.strip()]
    if spec.table_env:
        pinned = os.environ.get(spec.table_env, "").strip()
        if pinned:
            return [pinned]
    try:
        raw = fetch_json(f"TABLAS_OPERACION/{spec.operation}")
    except Exception:
        log.exception("INE table discovery failed for %s/%s", spec.operation, spec.indicator)
        return []
    tables = raw if isinstance(raw, list) else []
    ids = choose_tables(tables, spec) if spec.all_tables else (
        [t] if (t := choose_table(tables, spec)) else []
    )
    if not ids:
        env_hint = spec.tables_env or spec.table_env or "the table env"
        log.warning(
            "No INE table matched %s for %s (keywords=%s); set %s to pin one.",
            spec.operation, spec.indicator, spec.keywords, env_hint,
        )
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


def _upsert(rows: list[tuple[str, str, dt.date, float]]) -> int:
    with session_scope() as session:
        for offset in range(0, len(rows), _UPSERT_CHUNK):
            chunk = rows[offset : offset + _UPSERT_CHUNK]
            stmt = pg_insert(RegionObservation).values(
                [
                    {
                        "region_code": region,
                        "indicator": indicator,
                        "period": period,
                        "value": value,
                        "source": SOURCE,
                    }
                    for region, indicator, period, value in chunk
                ]
            )
            session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_region_obs_region_indicator_period",
                    set_={"value": stmt.excluded.value, "source": stmt.excluded.source},
                )
            )
    return len(rows)


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
    return _upsert(rows)


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
    SELECT p.region_code, 'densidad', p.period, p.value / s.value, 'derived'
    FROM region_observations p
    JOIN region_observations s
      ON s.region_code = p.region_code AND s.period = p.period
     AND s.indicator = 'superficie_km2' AND s.value > 0
    WHERE p.indicator = 'poblacion'
    ON CONFLICT (region_code, indicator, period)
      DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source
    """
)


def derive_density() -> int:
    """densidad = poblacion / superficie_km2, per region and matching period."""
    with session_scope() as session:
        session.execute(delete(RegionObservation).where(RegionObservation.indicator == "densidad"))
        result = session.execute(_DERIVE_DENSITY)
    return int(getattr(result, "rowcount", 0) or 0)


def ingest_ine() -> int:
    """Run every INE spec; clear sample rows for the indicators populated."""
    total = 0
    touched: set[str] = set()
    for spec in INE_SPECS:
        written = ingest_spec(spec)
        if written:
            touched.add(spec.indicator)
        total += written
    try:
        derive_density()
    except Exception:
        log.exception("Density derivation failed")
    if touched:
        clear_sample_observations(touched)
    return total


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_ine()
