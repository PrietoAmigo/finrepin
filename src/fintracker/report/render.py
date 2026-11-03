"""Render the weekly report as inline-styled HTML (email-safe) and plain text.

`sample_report.html` at the repo root is a static preview of this design.
"""

from __future__ import annotations

import html

from fintracker.report.data import PriceRow, Report

_CURRENCY_SYMBOLS = {"USD": "$", "CAD": "C$", "EUR": "€", "GBP": "£"}

_GREEN = "#137333"
_RED = "#a50e0e"
_GRAY = "#5f6368"
# Accent used to make the Stocks / Crypto / Forex section headers stand out.
_ACCENT = "#1967d2"
_ACCENT_BG = "#eef4fd"

_KIND_HEADERS = (
    ("equity", "Stocks"),
    ("index", "Indexes & funds"),
    ("metal", "Precious metals"),
    ("crypto", "Crypto"),
    ("forex", "Forex"),
)
_OTHER_HEADER = "Other"

_NO_HOLDINGS = "No open positions — record a buy on the Manage dashboard."
_NO_WATCHLIST = "Nothing on your watchlist — add symbols on the Manage dashboard."
_FALLBACK_NOTE = (
    "Your watchlist is empty, so this shows REPORT_SYMBOLS, or every tracked "
    "instrument you don't hold."
)


def _grouped(rows: list[PriceRow]) -> list[tuple[str, list[PriceRow]]]:
    """Rows bucketed under their kind's section header, in display order."""
    known = {kind for kind, _ in _KIND_HEADERS}
    groups = [(header, [r for r in rows if r.kind == kind]) for kind, header in _KIND_HEADERS]
    groups.append((_OTHER_HEADER, [r for r in rows if r.kind not in known]))
    return [(header, members) for header, members in groups if members]


def _sym(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency, f"{currency} ")


def _fmt_level(value: float, currency: str, is_score: bool = False, unit: str = "") -> str:
    if is_score:  # unitless (e.g. MVRV Z-Score): no currency symbol
        return f"{value:,.2f}"
    digits = 4 if abs(value) < 10 else 2
    return f"{_sym(currency)}{value:,.{digits}f}{unit}"


def _fmt_move(value: float | None, is_score: bool = False) -> tuple[str, str]:
    """Format a period move as (text, color). Percentage for prices, absolute
    delta for a unitless score."""
    if value is None:
        return "—", _GRAY
    color = _GREEN if value >= 0 else _RED
    return (f"{value:+.2f}" if is_score else f"{value:+.2f}%"), color


def _html_price_row(row: PriceRow) -> str:
    cells = []
    for move in (row.week_pct, row.month_pct, row.year_pct):
        text, color = _fmt_move(move, row.is_score)
        cells.append(f"<td style='padding:6px 8px;text-align:right;color:{color}'>{text}</td>")
    return (
        "<tr>"
        f"<td style='padding:6px 8px'><b>{html.escape(row.symbol)}</b>"
        f"<div style='color:{_GRAY};font-size:12px'>{html.escape(row.name)}</div></td>"
        f"<td style='padding:6px 8px;text-align:right;white-space:nowrap'>"
        f"{_fmt_level(row.level, row.currency, row.is_score, row.unit)}</td>"
        + "".join(cells)
        + "</tr>"
    )


def _html_note(text: str) -> str:
    return f"<p style='color:{_GRAY};font-size:13px;margin:0 0 8px'>{html.escape(text)}</p>"


def _html_section(title: str) -> str:
    return f"<h2 style='font-size:16px;margin:24px 0 8px;color:#202124'>{title}</h2>"


def _html_price_table(rows: list[PriceRow], lookback_days: int) -> str:
    parts = [
        "<table style='width:100%;border-collapse:collapse;font-size:14px'>",
        "<tr style='border-bottom:1px solid #e0e0e0'>"
        + "".join(
            # Fixed widths keep the Holdings and Watchlist columns lined up.
            f"<th style='text-align:{align};width:{width};padding:6px 8px;color:{_GRAY};"
            f"font-size:12px'>{label}</th>"
            for label, align, width in (
                ("Instrument", "left", "40%"),
                ("Level", "right", "21%"),
                (f"{lookback_days} days", "right", "13%"),
                ("1 month", "right", "13%"),
                ("1 year", "right", "13%"),
            )
        )
        + "</tr>",
    ]
    for header, members in _grouped(rows):
        parts.append(
            f"<tr><td colspan='5' style='padding:12px 8px;font-weight:700;color:{_ACCENT};"
            f"background:{_ACCENT_BG};border-bottom:2px solid {_ACCENT};font-size:15px;"
            "text-transform:uppercase;letter-spacing:.06em'>"
            f"{html.escape(header)}</td></tr>"
        )
        parts.extend(_html_price_row(row) for row in members)
    parts.append("</table>")
    return "".join(parts)


def _html_tag(held: bool) -> str:
    color = _ACCENT if held else _GRAY
    return (
        f" <span style='font-size:11px;color:{color};border:1px solid {color};"
        f"border-radius:3px;padding:0 4px'>{'holding' if held else 'watchlist'}</span>"
    )


def render_html(report: Report) -> str:
    parts: list[str] = []
    add = parts.append

    add(
        "<div style='font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "max-width:640px;margin:0 auto;color:#202124'>"
    )
    add("<h1 style='font-size:20px;margin:0 0 4px'>Weekly market report</h1>")
    add(
        f"<p style='color:{_GRAY};font-size:13px;margin:0 0 8px'>"
        f"Generated {report.generated_at:%A, %d %B %Y}.</p>"
    )

    add(_html_section("Holdings"))
    if report.holdings:
        add(_html_price_table(report.holdings, report.lookback_days))
    else:
        add(_html_note(_NO_HOLDINGS))

    add(_html_section("Watchlist"))
    if report.watchlist_is_fallback and report.watchlist:
        add(_html_note(_FALLBACK_NOTE))
    if report.watchlist:
        add(_html_price_table(report.watchlist, report.lookback_days))
    else:
        add(_html_note(_NO_WATCHLIST))

    add(_html_section("Upcoming earnings"))
    if report.earnings:
        add("<ul style='padding-left:18px;margin:0'>")
        for e in report.earnings:
            estimated = (
                f" <span style='color:{_GRAY};font-size:12px'>(estimated)</span>"
                if e.is_estimated
                else ""
            )
            add(
                f"<li style='margin:4px 0'><b>{e.date:%d %b %Y}</b> — "
                f"{html.escape(e.symbol)} <span style='color:{_GRAY}'>"
                f"{html.escape(e.name)}</span>{_html_tag(e.held)}{estimated}</li>"
            )
        add("</ul>")
    else:
        add(_html_note("Nothing scheduled."))

    add("</div>")
    return "".join(parts)


def _text_price_lines(rows: list[PriceRow], lookback_days: int) -> list[str]:
    lines: list[str] = []
    for header, members in _grouped(rows):
        lines += ["", f"  === {header.upper()} ==="]
        for row in members:
            week, _ = _fmt_move(row.week_pct, row.is_score)
            month, _ = _fmt_move(row.month_pct, row.is_score)
            year, _ = _fmt_move(row.year_pct, row.is_score)
            level = _fmt_level(row.level, row.currency, row.is_score, row.unit)
            lines.append(
                f"    {row.symbol:<12} {level:>17}  "
                f"{lookback_days}d {week:>8}  1m {month:>8}  1y {year:>8}"
            )
    return lines


def render_text(report: Report) -> str:
    lines: list[str] = [
        "WEEKLY MARKET REPORT",
        f"Generated {report.generated_at:%A, %d %B %Y}.",
        "",
        "HOLDINGS",
    ]
    if report.holdings:
        lines += _text_price_lines(report.holdings, report.lookback_days)
    else:
        lines.append(f"  {_NO_HOLDINGS}")

    lines += ["", "WATCHLIST"]
    if report.watchlist_is_fallback and report.watchlist:
        lines.append(f"  {_FALLBACK_NOTE}")
    if report.watchlist:
        lines += _text_price_lines(report.watchlist, report.lookback_days)
    else:
        lines.append(f"  {_NO_WATCHLIST}")

    lines += ["", "UPCOMING EARNINGS"]
    if report.earnings:
        for e in report.earnings:
            tag = "holding" if e.held else "watchlist"
            estimated = " (estimated)" if e.is_estimated else ""
            lines.append(f"  {e.date:%d %b %Y} - {e.symbol} {e.name} [{tag}]{estimated}")
    else:
        lines.append("  Nothing scheduled.")

    return "\n".join(lines)
