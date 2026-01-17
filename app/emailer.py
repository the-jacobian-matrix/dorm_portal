from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


class EmailNotConfiguredError(RuntimeError):
    pass


def ensure_email_configured() -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise EmailNotConfiguredError(
            "SMTP is not configured. Set SMTP_HOST and SMTP_FROM (and credentials if needed)."
        )


def send_email(*, to_email: str, subject: str, html_body: str, text_body: str) -> None:
    ensure_email_configured()

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message["Subject"] = subject

    # Plaintext + HTML alternative
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.ehlo()
        if settings.smtp_use_tls:
            smtp.starttls()
            smtp.ehlo()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
