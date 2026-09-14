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

import socket
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr


def _create_ipv4_connection(address, timeout, source_address=None):
    """Zelfde als socket.create_connection, maar dwingt IPv4 af. Render's
    netwerk heeft geen betrouwbare uitgaande IPv6-connectiviteit, en grote
    providers (bv. smtp.gmail.com) publiceren zowel A- als AAAA-records -
    zonder deze override kiest Python soms het IPv6-adres, wat dan faalt
    met '[Errno 101] Network is unreachable' (inconsistent: lukt de
    volgende poging soms wel, als de resolver toevallig IPv4 teruggeeft).
    Dit dwingt meteen IPv4 af i.p.v. te gokken."""
    host, port = address
    last_err = None
    for res in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        af, socktype, proto, _canonname, sockaddr = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_err = exc
            if sock is not None:
                sock.close()
    if last_err is not None:
        raise last_err
    raise OSError(f"Kon geen IPv4-adres vinden voor {host}")


class _IPv4SMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _create_ipv4_connection((host, port), timeout, self.source_address)


class _IPv4SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = _create_ipv4_connection((host, port), timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


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
    internal relays, etc.). Uses the IPv4-forcing subclasses above - Render
    has no reliable outbound IPv6, and dual-stack hosts like
    smtp.gmail.com would otherwise intermittently fail with
    '[Errno 101] Network is unreachable'."""
    host = settings["host"]
    port = int(settings["port"])

    if port == 465:
        server = _IPv4SMTP_SSL(host, port, timeout=20)
    else:
        server = _IPv4SMTP(host, port, timeout=20)
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
