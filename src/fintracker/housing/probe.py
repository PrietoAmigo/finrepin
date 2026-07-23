"""Read-only probe for INE's Tempus3 JSON API — inspect what a table or an
operation actually returns, so a new ``IneSpec`` can be written against real
labels instead of guessed ones.

Nothing here touches the database; it only fetches and prints. Use it to verify
the *candidate* series listed under "Housing data we could add next" in the
README before wiring them into ``INE_SPECS`` (or a ``MivauSpec`` / ``CensoSpec``).

List the tables of an operation (e.g. 15 = IPV), to find a table id::

    python -m fintracker.housing.probe op 15

Dump the series of a table, to see which label picks the measure you want
(e.g. 80270 = IPV general / new / second-hand, 76317 = mortgages)::

    python -m fintracker.housing.probe table 80270 --limit 40

Each printed series line is its composed label (``Nombre`` + ``MetaData``) — the
same text ``IneSpec.value_filters`` / ``exclude_values`` match against, so copy
the distinguishing substrings straight from the output.
"""

from __future__ import annotations

import argparse
import logging

from fintracker.config import get_settings
from fintracker.housing.ingest_ine import _labels, fetch_json

log = logging.getLogger(__name__)


def probe_operation(operation: str) -> list[tuple[str, str]]:
    """Return ``(table_id, title)`` for every table of an INE operation. Pure-ish
    (network read only)."""
    raw = fetch_json(f"TABLAS_OPERACION/{operation}")
    tables = raw if isinstance(raw, list) else []
    out: list[tuple[str, str]] = []
    for table in tables:
        table_id = table.get("Id")
        if table_id is not None:
            out.append((str(table_id), str(table.get("Nombre", ""))))
    return out


def probe_table(table_id: str, limit: int = 30) -> list[str]:
    """Return the composed label of each series in an INE table (capped at
    ``limit``); ``nult=1`` keeps the payload small."""
    raw = fetch_json(f"DATOS_TABLA/{table_id}", params={"det": 2, "nult": 1})
    series_list = raw if isinstance(raw, list) else []
    return [" · ".join(_labels(series)) for series in series_list[:limit]]


def _print_operation(operation: str) -> None:
    tables = probe_operation(operation)
    print(f"{len(tables)} table(s) in INE operation {operation}:")
    for table_id, title in tables:
        print(f"  {table_id:>8}  {title}")


def _print_table(table_id: str, limit: int) -> None:
    labels = probe_table(table_id, limit)
    print(f"first {len(labels)} series of INE table {table_id}:")
    for label in labels:
        print(f"  {label}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(message)s")
    parser = argparse.ArgumentParser(description="Inspect INE Tempus3 tables/operations.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    op = sub.add_parser("op", help="list the tables of an operation")
    op.add_argument("operation", help="INE operation code, e.g. 15")
    tb = sub.add_parser("table", help="list the series of a table")
    tb.add_argument("table_id", help="INE DATOS_TABLA id, e.g. 80270")
    tb.add_argument("--limit", type=int, default=30, help="max series to print (default 30)")
    args = parser.parse_args(argv)
    if args.cmd == "op":
        _print_operation(args.operation)
    else:
        _print_table(args.table_id, args.limit)


if __name__ == "__main__":
    main()
