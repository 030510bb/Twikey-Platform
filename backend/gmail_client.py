"""
Gmail API client for the Twikey Sales Platform backend.

Uses a Google Cloud service account with domain-wide delegation to send
and read mail on behalf of a specific Google Workspace mailbox
(e.g. sales@twikeycampaigns.nl), without any per-user OAuth consent flow.

Setup required before this works (see README.md for full steps):
  1. A Google Cloud project with the Gmail API enabled.
  2. A service account with a downloaded JSON key file.
  3. That service account's Client ID authorized for domain-wide delegation
     in the Workspace Admin console, with the scopes below.

Credentials can be provided two ways, so this works both locally and on a
cloud host where you generally cannot (and should not) commit a key file:
  - GOOGLE_SERVICE_ACCOUNT_FILE: a path to the JSON key file on disk.
    Used for local development, or for a "Secret File" mounted by the
    hosting platform (e.g. Render's Secret Files feature).
  - GOOGLE_SERVICE_ACCOUNT_JSON: the full JSON key content itself, pasted
    into a single environment variable. Used when the hosting platform only
    offers plain environment variables for secrets.
If both are set, GOOGLE_SERVICE_ACCOUNT_JSON takes precedence.
"""

import base64
import json
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText

from google.oauth2 import service_account
from googleapiclient.discovery import build

# Scopes requested by the app. These must match exactly what is authorized
# in the Workspace Admin console under "Domain-wide Delegation".
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _load_base_credentials() -> service_account.Credentials:
    """Load service-account credentials from a JSON env var or a file path."""
    json_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_env:
        try:
            info = json.loads(json_env)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON. "
                "Paste the full contents of the downloaded key file as-is."
            ) from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if service_account_file and os.path.exists(service_account_file):
        return service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )

    raise RuntimeError(
        "No Gmail credentials configured. Set either GOOGLE_SERVICE_ACCOUNT_JSON "
        "(the key file's full JSON content) or GOOGLE_SERVICE_ACCOUNT_FILE "
        "(a path to the key file). See README.md / DEPLOY.md for how to set these up."
    )


def _get_delegated_service(subject: str):
    """Build an authenticated Gmail API client impersonating `subject`."""
    credentials = _load_base_credentials()
    delegated_credentials = credentials.with_subject(subject)
    return build("gmail", "v1", credentials=delegated_credentials, cache_discovery=False)


def _send(send_as: str, to: str, subject: str, body: str, subtype: str = "plain") -> dict:
    service = _get_delegated_service(send_as)

    message = MIMEText(body, subtype)
    message["to"] = to
    message["from"] = send_as
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_email(send_as: str, to: str, subject: str, body: str) -> dict:
    """Send a plain-text email as `send_as`, returning the Gmail API response."""
    return _send(send_as, to, subject, body, subtype="plain")


def send_html_email(send_as: str, to: str, subject: str, html_body: str) -> dict:
    """
    Send an HTML email as `send_as`. Used for campaign sends that embed an
    open-tracking pixel and click-tracked links - both need real HTML,
    a plain-text message can't carry either.
    """
    return _send(send_as, to, subject, html_body, subtype="html")


def list_recent_messages(send_as: str, max_results: int = 10) -> dict:
    """
    Fetch the most recent inbox messages for `send_as`.

    Returns a dict with:
      - messages: list of {from, subject, date, unread}
      - unread_count: number of unread messages among the ones fetched
      - synced_today: number of the fetched messages received today (UTC)
    """
    service = _get_delegated_service(send_as)

    resp = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results, labelIds=["INBOX"])
        .execute()
    )
    message_refs = resp.get("messages", [])

    messages = []
    unread_count = 0
    synced_today = 0
    today = datetime.now(timezone.utc).date()

    for ref in message_refs:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        is_unread = "UNREAD" in msg.get("labelIds", [])
        if is_unread:
            unread_count += 1

        internal_ts_seconds = int(msg["internalDate"]) / 1000
        msg_date = datetime.fromtimestamp(internal_ts_seconds, tz=timezone.utc)
        if msg_date.date() == today:
            synced_today += 1

        messages.append(
            {
                "from": headers.get("From", "(onbekend)"),
                "subject": headers.get("Subject", "(geen onderwerp)"),
                "date": msg_date.isoformat(),
                "unread": is_unread,
            }
        )

    return {
        "messages": messages,
        "unread_count": unread_count,
        "synced_today": synced_today,
    }
