"""Build and send the weekly email report over SMTP (STARTTLS).

Run once by hand with:
    python -m fintracker.report.email_report
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.report.data import Report, build_report
from fintracker.report.render import render_html, render_text

log = logging.getLogger(__name__)


def _compose(report: Report) -> EmailMessage:
    settings = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"Weekly market report — {report.generated_at:%d %b %Y}"
    msg["From"] = settings.email_user
    msg["To"] = settings.email_to
    msg.set_content(render_text(report))
    msg.add_alternative(render_html(report), subtype="html")
    return msg


def send_weekly_report() -> bool:
    """Build the report from the DB and email it. Returns True if sent."""
    settings = get_settings()
    if not settings.email_configured:
        log.info("Email not configured (EMAIL_USER/EMAIL_PASS/EMAIL_TO) — skipping report.")
        return False

    with session_scope() as session:
        report = build_report(session)
    msg = _compose(report)

    log.info("Sending weekly report to %s via %s ...", settings.email_to, settings.email_host)
    context = ssl.create_default_context()
    with smtplib.SMTP(settings.email_host, settings.email_port, timeout=30) as smtp:
        smtp.starttls(context=context)
        smtp.login(settings.email_user, settings.email_pass)
        smtp.send_message(msg)
    log.info("Weekly report sent.")
    return True


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    send_weekly_report()
