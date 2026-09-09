"""
Generic SMTP client for sending mail through a customer's own mailbox.

This is the per-account counterpart to gmail_client.py: instead of every
account's campaign/manual emails going out through one shared Gmail mailbox
(SEND_AS_EMAIL), an account can configure its own SMTP credentials (see
POST /api/email-settings in app.py) so that mail goes out - and replies come
back - through their own domain and their own mailbox.

Uses Python's stdlib smtplib rather than a third-party mail API, since we
don't control which provider a customer's mailbox lives on: this works with
Gmail/Google Workspace (with an app password), Microsoft 365, Zoho, cPanel-
hosted mail, or basically any SMTP server, without per-provider integration
code.
"""

import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr


def _build_message(settings: dict, to: str, subject: str, body: str, subtype: str) -> MIMEText:
    msg = MIMEText(body, subtype)
    from_name = (settings.get("from_name") or "").strip()
    from_email = settings["from_email"]
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to
    msg["Subject"] = subject
    return msg


def _connect(settings: dict) -> smtplib.SMTP:
    """Open and authenticate an SMTP connection. Port 465 is implicit-TLS
    (SMTP_SSL); anything else (typically 587) starts in plaintext and
    upgrades with STARTTLS, unless use_tls is explicitly False (port 25,
    internal relays, etc.)."""
    host = settings["host"]
    port = int(settings["port"])

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        if settings.get("use_tls", True):
            server.starttls()

    server.login(settings["username"], settings["password"])
    return server


def send_email(settings: dict, to: str, subject: str, body: str, subtype: str = "plain") -> None:
    """
    settings: {host, port, username, password, from_email, from_name, use_tls}
    `password` must already be the decrypted plaintext (callers are
    responsible for calling crypto.decrypt() on the stored value first).
    Raises on any failure (auth, connect, send) - callers should catch and
    surface a readable message, same pattern as gmail_client's send_email.
    """
    msg = _build_message(settings, to, subject, body, subtype)
    server = _connect(settings)
    try:
        server.sendmail(settings["from_email"], [to], msg.as_string())
    finally:
        server.quit()


def send_html_email(settings: dict, to: str, subject: str, html_body: str) -> None:
    send_email(settings, to, subject, html_body, subtype="html")


def test_login(settings: dict) -> None:
    """Just the connect+login handshake, no message sent. Raises on failure.
    Used to validate settings quickly, but the /api/email-settings/test
    endpoint sends a real test email instead - a login can succeed while the
    from-address is still rejected by the provider, so an actual send is a
    more convincing check."""
    server = _connect(settings)
    server.quit()
