"""A small PC-Axis (``.px``) parser for INE census tables.

INE publishes the detailed *Censo de Población y Viviendas* housing
characteristics (mean floor area, year of construction, vacancy, tenure) only as
**PC-Axis ``.px``** files through its ``jaxi`` system — not as the Tempus3 JSON
that the rest of the housing ingest uses. This module parses that text format so
those series can be pulled too.

The ``.px`` format is a block of ``KEYWORD[("arg")]=value;`` metadata followed by
a ``DATA=`` matrix:

    STUB="Provincias","Tipo de vivienda";
    HEADING="Periodo";
    VALUES("Provincias")="28 Madrid","08 Barcelona";
    VALUES("Tipo de vivienda")="Total","Principal";
    VALUES("Periodo")="2021";
    DATA=
    1 2
    3 4
    ;

The data values are laid out over the cross-product of the STUB dimensions
(outer) and the HEADING dimensions (inner, last varies fastest) — the standard
PC-Axis ordering — so each value maps to one tuple of category labels.

The parser is pure (text in, structured data out); fetching a ``.px`` over HTTP
and mapping it to region observations lives in the ingest layer.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

# A keyword entry: KEYWORD or KEYWORD("sub") = "a","b",... ; (may span lines).
_ENTRY_RE = re.compile(
    r'([A-Z0-9\-]+)\s*(?:\(\s*"([^"]*)"\s*\))?\s*=\s*(.*?);',
    re.IGNORECASE | re.DOTALL,
)
# Missing-value markers PC-Axis uses in the DATA matrix.
_MISSING = {"..", ".", "-", ":", "...."}


def _split_quoted(raw: str) -> list[str]:
    """Split a metadata value into its quoted parts (``"a","b"`` → ``[a, b]``).

    Falls back to the stripped, unquoted scalar when there are no quotes (numeric
    keywords like ``DECIMALS=1``).
    """
    parts = re.findall(r'"([^"]*)"', raw)
    if parts:
        return parts
    scalar = raw.strip().strip('"').strip()
    return [scalar] if scalar else []


def cell_to_float(token: str) -> float | None:
    """A DATA token as a number, handling missing markers. Pure.

    PC-Axis DATA uses a period decimal and no thousands separators (unlike INE's
    Excel/HTML outputs), so this is a plain float parse; a lone comma is treated
    as a decimal defensively for any non-standard file.
    """
    text = token.strip().strip('"')
    if not text or text in _MISSING:
        return None
    text = text.replace("\xa0", "").replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class PxTable:
    """A parsed ``.px``: the ordered data dimensions and one value per cell.

    ``dims`` is STUB followed by HEADING (data order). ``categories`` maps each
    dimension to its ordered category labels. ``cells`` pairs every category
    tuple (aligned to ``dims``) with its value (``None`` when missing).
    """

    dims: tuple[str, ...]
    categories: dict[str, list[str]]
    cells: list[tuple[tuple[str, ...], float | None]]

    def series(self, **fixed: str) -> list[tuple[tuple[str, ...], float | None]]:
        """Cells whose category for a dimension equals the given value(s).

        ``table.series(**{"Tipo de vivienda": "Principal"})`` keeps only the
        principal-dwelling cells. Unknown dimension names raise ``KeyError``.
        """
        idx = {self.dims.index(dim): value for dim, value in fixed.items()}
        return [(labels, v) for labels, v in self.cells
                if all(labels[i] == value for i, value in idx.items())]


def _entries(text: str) -> dict[tuple[str, str | None], str]:
    """All ``KEYWORD[(sub)]=value`` entries up to (not including) DATA. Pure."""
    head = re.split(r'\bDATA\s*=', text, maxsplit=1, flags=re.IGNORECASE)[0]
    out: dict[tuple[str, str | None], str] = {}
    for keyword, sub, value in _ENTRY_RE.findall(head + ";"):
        out[(keyword.upper(), sub or None)] = value
    return out


def parse_px(text: str) -> PxTable:
    """Parse ``.px`` text into a :class:`PxTable`. Pure.

    Raises ``ValueError`` if the STUB/HEADING dimensions, their VALUES, or the
    DATA block are missing or inconsistent (e.g. the cell count doesn't match the
    product of the dimension sizes).
    """
    entries = _entries(text)
    stub = _split_quoted(entries.get(("STUB", None), ""))
    heading = _split_quoted(entries.get(("HEADING", None), ""))
    dims = [d for d in (*stub, *heading) if d]
    if not dims:
        raise ValueError("PC-Axis: no STUB/HEADING dimensions")
    categories: dict[str, list[str]] = {}
    for dim in dims:
        values = _split_quoted(entries.get(("VALUES", dim), ""))
        if not values:
            raise ValueError(f"PC-Axis: no VALUES for dimension {dim!r}")
        categories[dim] = values

    match = re.search(r'\bDATA\s*=(.*?);?\s*$', text, re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("PC-Axis: no DATA block")
    tokens = match.group(1).split()
    expected = 1
    for dim in dims:
        expected *= len(categories[dim])
    if len(tokens) != expected:
        raise ValueError(f"PC-Axis: {len(tokens)} data values, expected {expected}")

    combos = itertools.product(*(categories[dim] for dim in dims))
    cells = [(combo, cell_to_float(tok)) for combo, tok in zip(combos, tokens, strict=True)]
    return PxTable(dims=tuple(dims), categories=categories, cells=cells)
