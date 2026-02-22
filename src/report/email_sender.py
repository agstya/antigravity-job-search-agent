"""Gmail SMTP email sender for job match reports."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def send_report_email(
    html_body: str,
    text_body: str,
    subject: str,
    from_addr: str,
    to_addr: str,
    password: str,
) -> None:
    """Send an email report via Gmail SMTP with TLS.

    Args:
        html_body: HTML version of the report.
        text_body: Plain text fallback.
        subject: Email subject line.
        from_addr: Gmail address to send from.
        to_addr: Recipient email address.
        password: Gmail App Password (not your regular password).

    Raises:
        Exception: If email sending fails.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Job Search Agent <{from_addr}>"
    msg["To"] = to_addr

    # Attach plain text and HTML parts
    # Email clients will render the last part they can handle (HTML preferred)
    text_part = MIMEText(text_body, "plain", "utf-8")
    html_part = MIMEText(html_body, "html", "utf-8")
    msg.attach(text_part)
    msg.attach(html_part)

    logger.info("Connecting to Gmail SMTP (%s:%d)...", SMTP_SERVER, SMTP_PORT)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(from_addr, password)
        server.sendmail(from_addr, to_addr, msg.as_string())

    logger.info("Email sent successfully: '%s' → %s", subject, to_addr)
