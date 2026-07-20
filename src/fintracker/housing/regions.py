"""Canonical Spanish geography + housing-indicator registry.

The region list is derived from the ``es-atlas`` TopoJSON (the same geometry the
map renders), so every ``code`` here has a matching polygon in
``web/geo/spain-*.geojson`` and a stable INE code. ``name`` is kept identical to
the GeoJSON ``properties.name`` so the front-end can join map features to data by
name without a translation table.

Codes are namespaced by level because INE numbers autonomous communities and
provinces on independent 2-digit sequences (``13`` is both the *Madrid* community
and the *Ciudad Real* province): ``es`` (nation), ``ccaa-NN`` (autonomous
community), ``prov-NN`` (province).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str
    level: str  # nation | ccaa | prov
    ine: str  # INE 2-digit code within the level ("00" for the nation)
    name: str  # identical to the GeoJSON feature name
    parent: str | None


# (code, level, ine, name, parent) — generated from es-atlas, Gibraltar removed.
_REGION_ROWS: list[tuple[str, str, str, str, str | None]] = [
    ("es", "nation", "00", "España", None),
    ("ccaa-01", "ccaa", "01", "Andalucía", "es"),
    ("ccaa-02", "ccaa", "02", "Aragón", "es"),
    ("ccaa-03", "ccaa", "03", "Principado de Asturias", "es"),
    ("ccaa-04", "ccaa", "04", "Illes Balears", "es"),
    ("ccaa-05", "ccaa", "05", "Canarias", "es"),
    ("ccaa-06", "ccaa", "06", "Cantabria", "es"),
    ("ccaa-07", "ccaa", "07", "Castilla y León", "es"),
    ("ccaa-08", "ccaa", "08", "Castilla-La Mancha", "es"),
    ("ccaa-09", "ccaa", "09", "Cataluña/Catalunya", "es"),
    ("ccaa-10", "ccaa", "10", "Comunitat Valenciana", "es"),
    ("ccaa-11", "ccaa", "11", "Extremadura", "es"),
    ("ccaa-12", "ccaa", "12", "Galicia", "es"),
    ("ccaa-13", "ccaa", "13", "Comunidad de Madrid", "es"),
    ("ccaa-14", "ccaa", "14", "Región de Murcia", "es"),
    ("ccaa-15", "ccaa", "15", "Comunidad Foral de Navarra", "es"),
    ("ccaa-16", "ccaa", "16", "País Vasco/Euskadi", "es"),
    ("ccaa-17", "ccaa", "17", "La Rioja", "es"),
    ("ccaa-18", "ccaa", "18", "Ciudad Autónoma de Ceuta", "es"),
    ("ccaa-19", "ccaa", "19", "Ciudad Autónoma de Melilla", "es"),
    ("prov-01", "prov", "01", "Araba/Álava", "ccaa-16"),
    ("prov-02", "prov", "02", "Albacete", "ccaa-08"),
    ("prov-03", "prov", "03", "Alacant/Alicante", "ccaa-10"),
    ("prov-04", "prov", "04", "Almería", "ccaa-01"),
    ("prov-05", "prov", "05", "Ávila", "ccaa-07"),
    ("prov-06", "prov", "06", "Badajoz", "ccaa-11"),
    ("prov-07", "prov", "07", "Illes Balears", "ccaa-04"),
    ("prov-08", "prov", "08", "Barcelona", "ccaa-09"),
    ("prov-09", "prov", "09", "Burgos", "ccaa-07"),
    ("prov-10", "prov", "10", "Cáceres", "ccaa-11"),
    ("prov-11", "prov", "11", "Cádiz", "ccaa-01"),
    ("prov-12", "prov", "12", "Castelló/Castellón", "ccaa-10"),
    ("prov-13", "prov", "13", "Ciudad Real", "ccaa-08"),
    ("prov-14", "prov", "14", "Córdoba", "ccaa-01"),
    ("prov-15", "prov", "15", "A Coruña", "ccaa-12"),
    ("prov-16", "prov", "16", "Cuenca", "ccaa-08"),
    ("prov-17", "prov", "17", "Girona", "ccaa-09"),
    ("prov-18", "prov", "18", "Granada", "ccaa-01"),
    ("prov-19", "prov", "19", "Guadalajara", "ccaa-08"),
    ("prov-20", "prov", "20", "Gipuzkoa", "ccaa-16"),
    ("prov-21", "prov", "21", "Huelva", "ccaa-01"),
    ("prov-22", "prov", "22", "Huesca", "ccaa-02"),
    ("prov-23", "prov", "23", "Jaén", "ccaa-01"),
    ("prov-24", "prov", "24", "León", "ccaa-07"),
    ("prov-25", "prov", "25", "Lleida", "ccaa-09"),
    ("prov-26", "prov", "26", "La Rioja", "ccaa-17"),
    ("prov-27", "prov", "27", "Lugo", "ccaa-12"),
    ("prov-28", "prov", "28", "Madrid", "ccaa-13"),
    ("prov-29", "prov", "29", "Málaga", "ccaa-01"),
    ("prov-30", "prov", "30", "Murcia", "ccaa-14"),
    ("prov-31", "prov", "31", "Navarra", "ccaa-15"),
    ("prov-32", "prov", "32", "Ourense", "ccaa-12"),
    ("prov-33", "prov", "33", "Asturias", "ccaa-03"),
    ("prov-34", "prov", "34", "Palencia", "ccaa-07"),
    ("prov-35", "prov", "35", "Las Palmas", "ccaa-05"),
    ("prov-36", "prov", "36", "Pontevedra", "ccaa-12"),
    ("prov-37", "prov", "37", "Salamanca", "ccaa-07"),
    ("prov-38", "prov", "38", "Santa Cruz de Tenerife", "ccaa-05"),
    ("prov-39", "prov", "39", "Cantabria", "ccaa-06"),
    ("prov-40", "prov", "40", "Segovia", "ccaa-07"),
    ("prov-41", "prov", "41", "Sevilla", "ccaa-01"),
    ("prov-42", "prov", "42", "Soria", "ccaa-07"),
    ("prov-43", "prov", "43", "Tarragona", "ccaa-09"),
    ("prov-44", "prov", "44", "Teruel", "ccaa-02"),
    ("prov-45", "prov", "45", "Toledo", "ccaa-08"),
    ("prov-46", "prov", "46", "València/Valencia", "ccaa-10"),
    ("prov-47", "prov", "47", "Valladolid", "ccaa-07"),
    ("prov-48", "prov", "48", "Bizkaia", "ccaa-16"),
    ("prov-49", "prov", "49", "Zamora", "ccaa-07"),
    ("prov-50", "prov", "50", "Zaragoza", "ccaa-02"),
    ("prov-51", "prov", "51", "Ceuta", "ccaa-18"),
    ("prov-52", "prov", "52", "Melilla", "ccaa-19"),
]

REGIONS: list[Region] = [Region(*row) for row in _REGION_ROWS]
REGIONS_BY_CODE: dict[str, Region] = {r.code: r for r in REGIONS}


@dataclass(frozen=True)
class Indicator:
    code: str
    name: str  # short human label used in the UI selector
    description: str
    unit: str  # index | eur_m2 | count
    source: str  # INE | MIVAU
    base: str  # e.g. "2015=100" (index base or units note)
    frequency: str  # Q | M | A
    higher_is: str  # neutral | good | bad — colours the diverging YoY ramp legend


# INE House Price Index (Índice de Precios de Vivienda, IPV), base 2015=100,
# quarterly, published by autonomous community with a national total. Its three
# components — overall, new-build, and resale — are the three indicators here.
INDICATORS: list[Indicator] = [
    Indicator(
        code="ipv_general",
        name="House price index — Overall",
        description="INE House Price Index (IPV), all dwellings.",
        unit="index",
        source="INE",
        base="2015=100",
        frequency="Q",
        higher_is="neutral",
    ),
    Indicator(
        code="ipv_new",
        name="House price index — New",
        description="INE House Price Index (IPV), newly built dwellings.",
        unit="index",
        source="INE",
        base="2015=100",
        frequency="Q",
        higher_is="neutral",
    ),
    Indicator(
        code="ipv_secondhand",
        name="House price index — Resale",
        description="INE House Price Index (IPV), second-hand dwellings.",
        unit="index",
        source="INE",
        base="2015=100",
        frequency="Q",
        higher_is="neutral",
    ),
]
INDICATORS_BY_CODE: dict[str, Indicator] = {i.code: i for i in INDICATORS}


def normalize(text: str) -> str:
    """Accent-, case-, and punctuation-insensitive form for name matching."""
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    out = []
    for ch in stripped.lower():
        out.append(ch if ch.isalnum() or ch.isspace() else " ")
    return " ".join("".join(out).split())


# Distinctive normalized tokens per autonomous community, matched as substrings
# against whatever geographic label INE returns ("Madrid, Comunidad de",
# "Comunidad de Madrid", "Rioja, La", ...). Ordered so no token of one community
# is a substring of another's label; the nation is matched separately.
_CCAA_TOKENS: list[tuple[str, str]] = [
    ("ccaa-08", "mancha"),  # Castilla-La Mancha (before "castilla y leon")
    ("ccaa-07", "leon"),
    ("ccaa-01", "andalucia"),
    ("ccaa-02", "aragon"),
    ("ccaa-03", "asturias"),
    ("ccaa-04", "balear"),
    ("ccaa-05", "canarias"),
    ("ccaa-06", "cantabria"),
    ("ccaa-09", "catalu"),
    ("ccaa-10", "valencia"),
    ("ccaa-11", "extremadura"),
    ("ccaa-12", "galicia"),
    ("ccaa-13", "madrid"),
    ("ccaa-14", "murcia"),
    ("ccaa-15", "navarra"),
    ("ccaa-16", "vasco"),
    ("ccaa-16", "euskadi"),
    ("ccaa-17", "rioja"),
    ("ccaa-18", "ceuta"),
    ("ccaa-19", "melilla"),
]

# Exact national labels. "nacional" is deliberately NOT a bare substring token:
# it would also match table titles like "Índices nacionales por comunidades
# autónomas". "Total Nacional" is the phrase INE uses for the geographic total.
_NATION_EXACT = {"nacional", "espana", "total nacional", "nacional total", "total"}


def region_code_from_ine_name(name: str) -> str | None:
    """Map a single INE geographic label to a region ``code`` (or None).

    Returns ``"es"`` for the national total and ``"ccaa-NN"`` for a community.
    Province-level labels are not resolved here (the live ingest is CCAA-level).
    Pass one dimension value at a time (a metadata value or a dotted name
    segment), not a whole descriptive title.
    """
    norm = normalize(name)
    if norm in _NATION_EXACT or "total nacional" in norm:
        return "es"
    for code, token in _CCAA_TOKENS:
        if token in norm:
            return code
    return None
