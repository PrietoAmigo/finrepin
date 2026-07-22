"""Registry of the region time series tracked by the housing dashboard.

Independent of which regions carry each series. Drives the ``indicators`` seed,
the dashboard's indicator selector, and (loosely) what the ingestors fetch.
Units: ``eur_m2`` price per m², ``eur`` euros, ``count`` a number of things,
``m2`` area of a dwelling, ``km2`` territory area, ``year`` an age in years,
``inhab_km2`` density. Categories group them in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Indicator:
    code: str
    name: str
    unit: str
    source: str  # MIVAU | INE | derived
    frequency: str  # A | Q | M
    category: str  # price | income | demographic | housing | area
    higher_is: str = "neutral"


INDICATORS: list[Indicator] = [
    # --- Ministerio de Vivienda (MIVAU): free-market prices, €/m², quarterly ---
    Indicator("price_eur_m2", "House price, all (€/m²)", "eur_m2", "MIVAU", "Q", "price"),
    Indicator("price_eur_m2_new", "House price, new (€/m²)", "eur_m2", "MIVAU", "Q", "price"),
    Indicator("price_eur_m2_used", "House price, second-hand (€/m²)", "eur_m2", "MIVAU", "Q",
              "price"),
    # MIVAU's whole statistic is the appraised (tasado) value, so a separate
    # "appraisal" of the free-market price would duplicate price_eur_m2. This
    # fourth price slot is the distinct protected-housing (VPO) series instead.
    Indicator("price_eur_m2_protected", "House price, protected VPO (€/m²)", "eur_m2", "MIVAU", "Q",
              "price"),
    # --- INE: income (Atlas de distribución de renta de los hogares) ----------
    Indicator("renta_persona", "Net mean income per person (€)", "eur", "INE", "A", "income",
              "good"),
    Indicator("renta_hogar", "Net mean income per household (€)", "eur", "INE", "A", "income",
              "good"),
    # --- INE: demographics (Padrón / Cifras de población) ---------------------
    Indicator("poblacion", "Population (persons)", "count", "INE", "A", "demographic"),
    Indicator("superficie_km2", "Territory area (km²)", "km2", "INE", "A", "area"),
    Indicator("densidad", "Population density (inhab/km²)", "inhab_km2", "derived", "A",
              "demographic"),
    # --- INE: housing stock (Censo de Población y Viviendas) -------------------
    # ⚠️ Not wired into ingest_ine.INE_SPECS yet, so these stay empty (no
    # placeholder data is written). Dwelling COUNTS (viviendas_*) are available
    # via the Tempus3 JSON API (candidate table 3457) and only need a spec + id.
    # Mean floor area (superficie_media_m2) and mean age (antiguedad_media) are
    # published ONLY as PC-Axis (.px) census tables, which this JSON ingest
    # cannot read. superficie_km2 (and thus the derived densidad) likewise has
    # no clean Tempus3 series — see the README "Notes on data sources".
    Indicator("viviendas_total", "Dwellings (total)", "count", "INE", "A", "housing"),
    Indicator("viviendas_principales", "Main-residence dwellings", "count", "INE", "A", "housing"),
    Indicator("superficie_media_m2", "Mean dwelling floor area (m²)", "m2", "INE", "A", "housing"),
    Indicator("antiguedad_media", "Mean dwelling age (years)", "year", "INE", "A", "housing"),
    # --- Market activity ------------------------------------------------------
    # Demand/supply signals that sit alongside the €/m² prices. All are pinned by
    # env id (never auto-discovered) and stay empty until configured — see
    # ingest_ine.INE_SPECS / ingest_mivau.MIVAU_SPECS and the README.
    Indicator("compraventa", "Home sales (count)", "count", "INE", "M", "market"),
    Indicator("ipv", "House price index (2015=100)", "index", "INE", "Q", "market"),
    Indicator("precio_suelo_m2", "Urban land price (€/m²)", "eur_m2", "MIVAU", "Q", "market"),
]

INDICATORS_BY_CODE: dict[str, Indicator] = {i.code: i for i in INDICATORS}

# Price indicators are the ones the map colours by default (they exist at every
# level and are the headline series).
PRICE_INDICATORS: list[str] = [i.code for i in INDICATORS if i.category == "price"]
