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

_KIND_HEADERS = (("equity", "Stocks"), ("crypto", "Crypto"), ("forex", "Forex"))


def _sym(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency, f"{currency} ")


def _fmt_level(value: float, currency: str) -> str:
    digits = 4 if abs(value) < 10 else 2
    return f"{_sym(currency)}{value:,.{digits}f}"


def _fmt_pct(pct: float | None) -> tuple[str, str]:
    if pct is None:
        return "—", _GRAY
    return f"{pct:+.2f}%", (_GREEN if pct >= 0 else _RED)


def _html_price_row(row: PriceRow) -> str:
    cells = []
    for pct in (row.week_pct, row.month_pct, row.year_pct):
        text, color = _fmt_pct(pct)
        cells.append(f"<td style='padding:6px 8px;text-align:right;color:{color}'>{text}</td>")
    return (
        "<tr>"
        f"<td style='padding:6px 8px'><b>{html.escape(row.symbol)}</b>"
        f"<div style='color:{_GRAY};font-size:12px'>{html.escape(row.name)}</div></td>"
        f"<td style='padding:6px 8px;text-align:right;white-space:nowrap'>"
        f"{_fmt_level(row.level, row.currency)}</td>" + "".join(cells) + "</tr>"
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

    # Prices
    add("<h2 style='font-size:16px;margin:24px 0 8px;color:#202124'>Prices</h2>")
    add("<table style='width:100%;border-collapse:collapse;font-size:14px'>")
    add(
        "<tr style='border-bottom:1px solid #e0e0e0'>"
        + "".join(
            f"<th style='text-align:{align};padding:6px 8px;color:{_GRAY};font-size:12px'>"
            f"{label}</th>"
            for label, align in (
                ("Instrument", "left"),
                ("Level", "right"),
                (f"{report.lookback_days} days", "right"),
                ("1 month", "right"),
                ("1 year", "right"),
            )
        )
        + "</tr>"
    )
    for kind, header in _KIND_HEADERS:
        rows = [r for r in report.prices if r.kind == kind]
        if not rows:
            continue
        add(
            f"<tr><td colspan='5' style='padding:14px 8px 4px;font-weight:600;color:{_GRAY};"
            "font-size:12px;text-transform:uppercase;letter-spacing:.04em'>"
            f"{header}</td></tr>"
        )
        for row in rows:
            add(_html_price_row(row))
    add("</table>")

    # Upcoming earnings
    add("<h2 style='font-size:16px;margin:24px 0 8px;color:#202124'>Upcoming earnings</h2>")
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
                f"{html.escape(e.name)}</span>{estimated}</li>"
            )
        add("</ul>")
    else:
        add(f"<p style='color:{_GRAY};font-size:13px;margin:0'>Nothing scheduled.</p>")

    add("</div>")
    return "".join(parts)


def render_text(report: Report) -> str:
    lines: list[str] = [
        "WEEKLY MARKET REPORT",
        f"Generated {report.generated_at:%A, %d %B %Y}.",
        "",
        "PRICES",
    ]
    for kind, header in _KIND_HEADERS:
        rows = [r for r in report.prices if r.kind == kind]
        if not rows:
            continue
        lines.append(f"  {header}:")
        for row in rows:
            week, _ = _fmt_pct(row.week_pct)
            month, _ = _fmt_pct(row.month_pct)
            year, _ = _fmt_pct(row.year_pct)
            lines.append(
                f"    {row.symbol:<10} {_fmt_level(row.level, row.currency):>14}  "
                f"{report.lookback_days}d {week:>8}  1m {month:>8}  1y {year:>8}"
            )

    lines += ["", "UPCOMING EARNINGS"]
    if report.earnings:
        for e in report.earnings:
            estimated = " (estimated)" if e.is_estimated else ""
            lines.append(f"  {e.date:%d %b %Y} - {e.symbol} {e.name}{estimated}")
    else:
        lines.append("  Nothing scheduled.")

    return "\n".join(lines)
