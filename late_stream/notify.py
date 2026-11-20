"""Email alerts over SMTP."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)


class EmailError(RuntimeError):
    pass


def recipients(settings: dict) -> list[str]:
    return [a.strip() for a in str(settings.get("alert_to", "")).replace(";", ",").split(",")
            if a.strip()]


def is_configured(settings: dict) -> bool:
    return bool(settings.get("smtp_host") and recipients(settings))


def send_email(settings: dict, subject: str, body: str) -> None:
    """Send one email. Raises EmailError on failure."""
    to = recipients(settings)
    if not settings.get("smtp_host") or not to:
        raise EmailError("email is not configured (SMTP host and recipients are required)")
    msg = EmailMessage()
    msg["Subject"] = f"[late-stream] {subject}"
    msg["From"] = settings.get("smtp_from") or settings.get("smtp_user") or "late-stream@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    host = settings["smtp_host"]
    port = int(settings.get("smtp_port") or 587)
    security = settings.get("smtp_security", "starttls")
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if settings.get("smtp_user"):
                server.login(settings["smtp_user"], settings.get("smtp_password", ""))
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"could not send email via {host}:{port}: {e}") from e


class Notifier:
    """Sends alerts without ever raising (a broken mail server must not break
    a live stream); failures are logged."""

    def __init__(self, settings_getter, event_logger=None):
        self._settings = settings_getter
        self._event = event_logger

    def send(self, subject: str, body: str, stream_id: int | None = None,
             kind: str = "alert") -> bool:
        s = self._settings()
        if kind == "start" and not s.get("notify_start"):
            return False
        if kind == "finish" and not s.get("notify_finish"):
            return False
        if not is_configured(s):
            log.info("email not configured; would have sent: %s", subject)
            return False
        try:
            send_email(s, subject, body)
            return True
        except EmailError as e:
            log.error("%s", e)
            if self._event:
                self._event(stream_id, "error", str(e))
            return False
