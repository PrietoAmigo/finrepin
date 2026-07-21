"""Spanish region hierarchy (nation → CCAA → province → municipality).

The list is generated from the ``es-atlas`` geometry (``data/regions_all.json``),
so every region has a matching map polygon and an official INE code. It's the
single source seeded into the ``regions`` table and used to resolve the
geographic labels INE returns to region codes.

Codes are namespaced by level (INE numbers levels independently): ``es``,
``ccaa-NN``, ``prov-NN``, ``muni-NNNNN``.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str
    level: str  # nation | ccaa | prov | muni
    ine: str
    name: str
    parent: str | None
    lat: float | None
    lon: float | None


@functools.lru_cache(maxsize=1)
def _rows() -> list[dict]:
    resource = importlib.resources.files("fintracker.housing") / "data" / "regions_all.json"
    with resource.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@functools.lru_cache(maxsize=1)
def all_regions() -> list[Region]:
    return [
        Region(
            code=r["code"],
            level=r["level"],
            ine=r["ine"],
            name=r["name"],
            parent=r.get("parent"),
            lat=r.get("lat"),
            lon=r.get("lon"),
        )
        for r in _rows()
    ]


@functools.lru_cache(maxsize=1)
def regions_by_code() -> dict[str, Region]:
    return {r.code: r for r in all_regions()}


def regions_at(level: str) -> list[Region]:
    return [r for r in all_regions() if r.level == level]


def normalize(text: str) -> str:
    """Accent-, case-, and punctuation-insensitive form for name matching."""
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    out = [ch if ch.isalnum() or ch.isspace() else " " for ch in stripped.lower()]
    return " ".join("".join(out).split())


# --- INE geographic-label → region code resolution ---------------------------

# Distinctive normalized tokens per autonomous community (INE returns names like
# "Madrid, Comunidad de" / "Rioja, La"). Ordered so no token is a substring of
# another community's label.
_CCAA_TOKENS: list[tuple[str, str]] = [
    ("ccaa-08", "mancha"),
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
_NATION_EXACT = {"nacional", "espana", "total nacional", "nacional total", "total"}


def _token_key(text: str) -> str:
    """Order-insensitive key: sorted normalized tokens.

    Makes "Coruña, A" and "A Coruña", or "Rioja, La" and "La Rioja", match.
    """
    return " ".join(sorted(normalize(text).split()))


@functools.lru_cache(maxsize=1)
def _province_name_index() -> dict[str, str]:
    """Token-key of each province-name variant → province code.

    Indexes the whole name and each bilingual slash part ("Araba/Álava" →
    "Araba", "Álava"), keyed order-insensitively so INE's inverted forms match.
    """
    index: dict[str, str] = {}
    for region in regions_at("prov"):
        variants = {region.name, *region.name.split("/")}
        for variant in variants:
            key = _token_key(variant)
            if key:
                index.setdefault(key, region.code)
    return index


def region_code_from_ine_name(name: str, level: str = "ccaa") -> str | None:
    """Resolve an INE geographic label to a region code (or None).

    ``level`` selects the target granularity: ``nation``/``ccaa`` use token
    matching; ``prov`` uses the province name index. Municipal data is resolved
    by INE code, not name (see ``region_code_from_ine_code``).
    """
    norm = normalize(name)
    if not norm:
        return None
    if level in ("nation", "ccaa") and (norm in _NATION_EXACT or "total nacional" in norm):
        return "es"
    if level == "prov":
        index = _province_name_index()
        for part in {name, *name.split("/")}:
            code = index.get(_token_key(part))
            if code:
                return code
        return None
    for code, token in _CCAA_TOKENS:
        if token in norm:
            return code
    return None


def region_codes_for_name(name: str) -> list[str]:
    """All region codes a label matches across nation/CCAA/province levels.

    A single MIVAU price sheet lists the nation, communities and provinces
    together, so one row can feed several levels — e.g. "Madrid" is both
    province ``prov-28`` and community ``ccaa-13`` (same value for a
    single-province community). Municipalities are resolved by code, not here.
    """
    codes: list[str] = []
    for level in ("prov", "ccaa"):  # the "ccaa" pass also resolves the nation
        code = region_code_from_ine_name(name, level)
        if code and code not in codes:
            codes.append(code)
    return codes


def region_code_from_ine_code(ine_code: str, level: str) -> str | None:
    """Resolve an INE numeric code (2-digit CCAA/province, 5-digit municipality)
    to a namespaced region code, if that region exists."""
    prefix = {"ccaa": "ccaa", "prov": "prov", "muni": "muni", "nation": None}.get(level)
    if prefix is None:
        return "es" if ine_code in ("00", "0", "") else None
    width = 5 if level == "muni" else 2
    code = f"{prefix}-{ine_code.zfill(width)}"
    return code if code in regions_by_code() else None
