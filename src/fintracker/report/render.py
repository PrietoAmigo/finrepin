"""Render the weekly report as inline-styled HTML (email-safe) and plain text.

`sample_report.html` at the repo root is a static preview of this design.
"""

from __future__ import annotations

import html

from fintracker.report.data import FactHighlight, PriceRow, Report

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


def _fmt_big(value: float, currency: str = "USD") -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{sign}{_sym(currency)}{magnitude / 1e9:,.2f}B"
    if magnitude >= 1e6:
        return f"{sign}{_sym(currency)}{magnitude / 1e6:,.2f}M"
    return f"{sign}{_sym(currency)}{magnitude:,.2f}"


def _fmt_fact(fact: FactHighlight) -> str:
    if fact.unit == "USD/shares":
        return f"${fact.value:,.2f}"
    if fact.unit in _CURRENCY_SYMBOLS:
        return _fmt_big(fact.value, fact.unit)
    return f"{fact.value:,.2f} {fact.unit}"


def _fmt_pct(pct: float | None) -> tuple[str, str]:
    if pct is None:
        return "—", _GRAY
    return f"{pct:+.2f}%", (_GREEN if pct >= 0 else _RED)


def _html_price_row(row: PriceRow) -> str:
    day, day_color = _fmt_pct(row.day_pct)
    week, week_color = _fmt_pct(row.week_pct)
    return (
        "<tr>"
        f"<td style='padding:6px 8px'><b>{html.escape(row.symbol)}</b>"
        f"<div style='color:{_GRAY};font-size:12px'>{html.escape(row.name)}</div></td>"
        f"<td style='padding:6px 8px;text-align:right;white-space:nowrap'>"
        f"{_fmt_level(row.level, row.currency)}</td>"
        f"<td style='padding:6px 8px;text-align:right;color:{day_color}'>{day}</td>"
        f"<td style='padding:6px 8px;text-align:right;color:{week_color}'>{week}</td>"
        "</tr>"
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
        f"Generated {report.generated_at:%A, %d %B %Y}. "
        f"Moves shown over the last {report.lookback_days} days.</p>"
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
                ("1 day", "right"),
                ("Week", "right"),
            )
        )
        + "</tr>"
    )
    for kind, header in _KIND_HEADERS:
        rows = [r for r in report.prices if r.kind == kind]
        if not rows:
            continue
        add(
            f"<tr><td colspan='4' style='padding:14px 8px 4px;font-weight:600;color:{_GRAY};"
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

    # New filings
    add(
        "<h2 style='font-size:16px;margin:24px 0 8px;color:#202124'>"
        f"New filings (last {report.lookback_days} days)</h2>"
    )
    if report.filings:
        for filing in report.filings:
            period = (
                f" · period end {filing.period_end:%d %b %Y}" if filing.period_end else ""
            )
            add(
                "<div style='padding:8px 0;border-bottom:1px solid #eee'>"
                f"<b>{html.escape(filing.symbol)}</b> {html.escape(filing.form)} "
                f"<span style='color:{_GRAY};font-size:12px'>"
                f"filed {filing.filed_at:%d %b %Y}{period}</span>"
            )
            if filing.facts:
                add("<div style='margin-top:4px'>")
                for fact in filing.facts:
                    add(
                        "<span style='display:inline-block;margin:2px 12px 2px 0'>"
                        f"<span style='color:{_GRAY};font-size:12px'>"
                        f"{html.escape(fact.label)}</span> <b>{_fmt_fact(fact)}</b></span>"
                    )
                add("</div>")
            add("</div>")
    else:
        add(f"<p style='color:{_GRAY};font-size:13px;margin:0'>No new filings.</p>")

    add(
        f"<p style='margin:24px 0 0'><a href='{html.escape(report.grafana_url, quote=True)}' "
        "style='color:#1a73e8'>Open charts in Grafana →</a></p>"
    )
    add("</div>")
    return "".join(parts)


def render_text(report: Report) -> str:
    lines: list[str] = [
        "WEEKLY MARKET REPORT",
        f"Generated {report.generated_at:%A, %d %B %Y}. "
        f"Moves shown over the last {report.lookback_days} days.",
        "",
        "PRICES",
    ]
    for kind, header in _KIND_HEADERS:
        rows = [r for r in report.prices if r.kind == kind]
        if not rows:
            continue
        lines.append(f"  {header}:")
        for row in rows:
            day, _ = _fmt_pct(row.day_pct)
            week, _ = _fmt_pct(row.week_pct)
            lines.append(
                f"    {row.symbol:<10} {_fmt_level(row.level, row.currency):>14}  "
                f"1d {day:>8}  week {week:>8}"
            )

    lines += ["", "UPCOMING EARNINGS"]
    if report.earnings:
        for e in report.earnings:
            estimated = " (estimated)" if e.is_estimated else ""
            lines.append(f"  {e.date:%d %b %Y} - {e.symbol} {e.name}{estimated}")
    else:
        lines.append("  Nothing scheduled.")

    lines += ["", f"NEW FILINGS (LAST {report.lookback_days} DAYS)"]
    if report.filings:
        for filing in report.filings:
            period = f", period end {filing.period_end:%d %b %Y}" if filing.period_end else ""
            lines.append(
                f"  {filing.symbol} {filing.form} filed {filing.filed_at:%d %b %Y}{period}"
            )
            for fact in filing.facts:
                lines.append(f"    {fact.label}: {_fmt_fact(fact)}")
    else:
        lines.append("  No new filings.")

    lines += ["", f"Charts: {report.grafana_url}"]
    return "\n".join(lines)
