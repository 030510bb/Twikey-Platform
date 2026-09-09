"""
IMAP client for reading replies from a customer's own mailbox (Fase 2:
reply-tracking, see crm-roadmap.md). Counterpart to smtp_client.py
(sending) - uses the same per-account "bring your own mailbox" credentials
(smtp_settings.imap_* in database.py), read via stdlib imaplib so any IMAP
server works, not just one provider.

Deliberately read-only: every fetch below opens the mailbox with
readonly=True, and it never deletes/moves/flags a message it reads - a
customer's own inbox is theirs, this only ever looks at it.
"""

import email
import imaplib
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime


def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, encoding in parts:
        if isinstance(text, bytes):
            decoded += text.decode(encoding or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _extract_body(msg) -> str:
    """Prefers the plain-text part; falls back to a crude HTML-tag strip if
    that's all a message has."""
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace").strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return re.sub("<[^<]+?>", " ", payload.decode(charset, errors="replace")).strip()
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()


def fetch_new_messages(settings: dict, since_uid: int = 0) -> list:
    """
    settings: {imap_host, imap_port, imap_username, imap_password, imap_use_ssl}
    `imap_password` must already be decrypted plaintext (callers are
    responsible for crypto.decrypt() first, same convention as
    smtp_client.py).

    Returns [{uid, from_email, subject, body, received_at}, ...] for every
    message with a UID greater than since_uid, oldest first. Skips (rather
    than raises on) an individual malformed message, so one bad message in
    a mailbox never blocks reading the rest.
    """
    host = settings["imap_host"]
    port = settings.get("imap_port") or (993 if settings.get("imap_use_ssl", True) else 143)
    conn = imaplib.IMAP4_SSL(host, port) if settings.get("imap_use_ssl", True) else imaplib.IMAP4(host, port)
    results = []
    try:
        conn.login(settings["imap_username"], settings["imap_password"])
        conn.select("INBOX", readonly=True)
        status, data = conn.uid("search", None, "ALL")
        if status != "OK":
            return []
        uids = [int(u) for u in data[0].split()] if data and data[0] else []
        for uid in sorted(u for u in uids if u > since_uid):
            status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            try:
                msg = email.message_from_bytes(raw)
                _, from_email = parseaddr(msg.get("From", ""))
                subject = _decode(msg.get("Subject", ""))
                body = _extract_body(msg)
                try:
                    received_at = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat()
                except Exception:
                    received_at = datetime.now(timezone.utc).isoformat()
                results.append({
                    "uid": uid,
                    "from_email": from_email.lower(),
                    "subject": subject,
                    "body": body,
                    "received_at": received_at,
                })
            except Exception:
                continue
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return results


def test_login(settings: dict) -> int:
    """Just the connect+login+select handshake - returns the mailbox
    message count. Raises on failure. Used by
    POST /api/email-settings/test-imap in app.py."""
    host = settings["imap_host"]
    port = settings.get("imap_port") or (993 if settings.get("imap_use_ssl", True) else 143)
    conn = imaplib.IMAP4_SSL(host, port) if settings.get("imap_use_ssl", True) else imaplib.IMAP4(host, port)
    try:
        conn.login(settings["imap_username"], settings["imap_password"])
        status, mailboxes = conn.select("INBOX", readonly=True)
        return int(mailboxes[0]) if status == "OK" and mailboxes and mailboxes[0] else 0
    finally:
        conn.logout()
