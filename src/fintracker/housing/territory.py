"""Territory area (km²) reference data — the one housing input with no live API.

INE's Tempus3 exposes population but not a clean province-area series, so
``superficie_km2`` (and the ``densidad`` derived from it) sat empty. Area is a
fixed geographic fact, not a measurement that needs fetching, so we seed it from
the official figures: the 52 provinces, with autonomous-community and national
totals summed up the hierarchy (area is additive). ``derive_density`` in
``ingest_ine`` then divides population by it.

Figures are the official provincial surfaces in km² (INE "Superficie, población
y densidad"; IGN cartography), rounded to the km². They change only on a rare
boundary revision, so a static table is the honest source here — edit a value if
INE republishes it.

Run one off-schedule seed by hand with:
    python -m fintracker.housing.territory
"""

from __future__ import annotations

import datetime as dt
import logging

from fintracker.housing.regions import regions_at
from fintracker.housing.store import Observation, upsert_observations

log = logging.getLogger(__name__)

SOURCE = "IGN"
INDICATOR = "superficie_km2"
# Area barely changes; stamp one reference period. density joins on each region's
# latest area regardless of period, so this date is not load-bearing.
PERIOD = dt.date(2021, 1, 1)

# Province code (INE 2-digit, namespaced) → surface in km².
PROVINCE_AREA_KM2: dict[str, float] = {
    "prov-01": 3037.0,   # Araba/Álava
    "prov-02": 14926.0,  # Albacete
    "prov-03": 5817.0,   # Alicante/Alacant
    "prov-04": 8775.0,   # Almería
    "prov-05": 8050.0,   # Ávila
    "prov-06": 21766.0,  # Badajoz
    "prov-07": 4992.0,   # Balears, Illes
    "prov-08": 7726.0,   # Barcelona
    "prov-09": 14292.0,  # Burgos
    "prov-10": 19868.0,  # Cáceres
    "prov-11": 7436.0,   # Cádiz
    "prov-12": 6632.0,   # Castellón/Castelló
    "prov-13": 19813.0,  # Ciudad Real
    "prov-14": 13771.0,  # Córdoba
    "prov-15": 7950.0,   # Coruña, A
    "prov-16": 17141.0,  # Cuenca
    "prov-17": 5910.0,   # Girona
    "prov-18": 12647.0,  # Granada
    "prov-19": 12214.0,  # Guadalajara
    "prov-20": 1980.0,   # Gipuzkoa
    "prov-21": 10128.0,  # Huelva
    "prov-22": 15671.0,  # Huesca
    "prov-23": 13496.0,  # Jaén
    "prov-24": 15581.0,  # León
    "prov-25": 12172.0,  # Lleida
    "prov-26": 5045.0,   # Rioja, La
    "prov-27": 9856.0,   # Lugo
    "prov-28": 8028.0,   # Madrid
    "prov-29": 7308.0,   # Málaga
    "prov-30": 11313.0,  # Murcia
    "prov-31": 10391.0,  # Navarra
    "prov-32": 7273.0,   # Ourense
    "prov-33": 10604.0,  # Asturias
    "prov-34": 8052.0,   # Palencia
    "prov-35": 4066.0,   # Palmas, Las
    "prov-36": 4495.0,   # Pontevedra
    "prov-37": 12350.0,  # Salamanca
    "prov-38": 3381.0,   # Santa Cruz de Tenerife
    "prov-39": 5321.0,   # Cantabria
    "prov-40": 6923.0,   # Segovia
    "prov-41": 14036.0,  # Sevilla
    "prov-42": 10306.0,  # Soria
    "prov-43": 6303.0,   # Tarragona
    "prov-44": 14810.0,  # Teruel
    "prov-45": 15370.0,  # Toledo
    "prov-46": 10807.0,  # Valencia/València
    "prov-47": 8110.0,   # Valladolid
    "prov-48": 2217.0,   # Bizkaia
    "prov-49": 10561.0,  # Zamora
    "prov-50": 17275.0,  # Zaragoza
    "prov-51": 19.0,     # Ceuta
    "prov-52": 13.0,     # Melilla
}


def area_rows() -> list[Observation]:
    """(region_code, indicator, period, value) for provinces + summed CCAA + nation.

    Area is additive, so each CCAA is the sum of its provinces and the nation is
    the sum of all provinces — derived from the seeded hierarchy, not hardcoded.
    Pure.
    """
    rows: list[Observation] = [
        (code, INDICATOR, PERIOD, area) for code, area in PROVINCE_AREA_KM2.items()
    ]
    ccaa_totals: dict[str, float] = {}
    for prov in regions_at("prov"):
        area = PROVINCE_AREA_KM2.get(prov.code)
        if area is not None and prov.parent:
            ccaa_totals[prov.parent] = ccaa_totals.get(prov.parent, 0.0) + area
    rows.extend((code, INDICATOR, PERIOD, total) for code, total in ccaa_totals.items())
    rows.append(("es", INDICATOR, PERIOD, sum(PROVINCE_AREA_KM2.values())))
    return rows


def seed_territory_area() -> int:
    """Upsert the static territory-area series (idempotent). Returns rows written."""
    written = upsert_observations(area_rows(), SOURCE)
    log.info("Seeded %d territory-area rows (superficie_km2).", written)
    return written


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    seed_territory_area()
