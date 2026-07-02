"""
Sends the weekly digest email via Gmail SMTP (App Password).
Skips silently if DIGEST_EMAIL_PASS is not configured.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.config import DIGEST_EMAIL_PASS, DIGEST_FROM, DIGEST_TO
from modules.digest.builder import build_digest

log = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def send_digest() -> dict:
    """Build and send the weekly digest. Returns a status dict."""
    if not DIGEST_EMAIL_PASS:
        log.info("digest: DIGEST_EMAIL_PASS not set — skipping email")
        return {"ok": False, "reason": "email not configured"}

    if not DIGEST_FROM or not DIGEST_TO:
        log.warning("digest: DIGEST_FROM or DIGEST_TO missing")
        return {"ok": False, "reason": "sender/recipient not configured"}

    try:
        subject, text_body, html_body = build_digest()
    except Exception as e:
        log.error("digest build failed: %s", e, exc_info=True)
        return {"ok": False, "reason": f"build failed: {e}"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = DIGEST_FROM
    msg["To"]      = DIGEST_TO
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(DIGEST_FROM, DIGEST_EMAIL_PASS)
            smtp.sendmail(DIGEST_FROM, DIGEST_TO, msg.as_string())
        log.info("digest: sent to %s (subject: %s)", DIGEST_TO, subject)
        return {"ok": True, "subject": subject, "to": DIGEST_TO}
    except smtplib.SMTPAuthenticationError:
        log.error("digest: Gmail authentication failed — check DIGEST_EMAIL_PASS App Password")
        from core.mistake_log import log_service_failure
        log_service_failure("email_smtp", "Gmail authentication failed — check App Password", severity="high")
        return {"ok": False, "reason": "auth failed"}
    except Exception as e:
        log.error("digest: SMTP error: %s", e, exc_info=True)
        from core.mistake_log import log_service_failure
        log_service_failure("email_smtp", f"SMTP error: {e}", severity="high")
        return {"ok": False, "reason": str(e)}
