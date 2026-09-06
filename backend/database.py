"""
Lightweight SQLite storage for the Twikey Sales Platform backend.

This backs every account's Contacts, A/B Test / Analytics and
LinkedIn-tracking data, plus the login system itself (accounts + sessions).

Why SQLite: it needs zero external services or credentials, so everything in
this project still runs with just "GOOGLE_SERVICE_ACCOUNT_JSON" configured -
no extra account to create before you can try this out.

Important limitation on Render's free plan: the free web service's disk is
ephemeral. Data written here survives restarts/sleep, but is WIPED on every
new deploy (e.g. every `git push`) - including accounts/passwords. That is
fine for testing, but before you rely on this for real customer data, move
to a real database - Render's Postgres (a free instance is available for the
first 30 days) or the Supabase project you already have are both a drop-in
fit for the table layout below. This file isolates all SQL in one place
specifically to make that swap easy later.

Multi-tenancy: every account (= one customer) only ever sees its own
contacts/campaigns/LinkedIn data - every query below that touches those
tables is scoped by account_id. One thing is NOT tenant-isolated yet: email
sending/reading still goes through the single shared Gmail mailbox
configured via SEND_AS_EMAIL in app.py. Giving each customer their own
sending identity would mean each one configuring their own Google Workspace
domain + service account (the whole README.md setup, repeated per
customer) - a real next step, not built here.
"""

import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import bcrypt

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "twikey_platform.db"))

SESSION_LIFETIME_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    login_email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    first_name TEXT NOT NULL,
    last_name TEXT DEFAULT '',
    email TEXT NOT NULL,
    company TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(account_id, email)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    launched_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    group_label TEXT NOT NULL,
    offer_name TEXT NOT NULL,
    subject_template TEXT NOT NULL,
    body_template TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    variant_id INTEGER NOT NULL REFERENCES campaign_variants(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    tracking_token TEXT NOT NULL UNIQUE,
    sent_at TEXT,
    send_error TEXT,
    opened_at TEXT,
    clicked_at TEXT,
    form_filled_at TEXT
);

CREATE TABLE IF NOT EXISTS linkedin_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    label TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linkedin_outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    contact_id INTEGER REFERENCES contacts(id),
    contact_name TEXT NOT NULL,
    action TEXT NOT NULL,
    template_label TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""

DEFAULT_LINKEDIN_TEMPLATES = [
    (
        "A",
        "Short & Direct",
        "Hi {{firstName}}, saw you're with {{company}}. Quick question about payment "
        "collection timing - interested in a chat?",
    ),
    (
        "B",
        "Value-Focused",
        "{{firstName}}, most finance teams at {{company}}'s size are looking to reduce "
        "DSO. Worth exploring what's possible?",
    ),
    (
        "C",
        "Question-Based",
        "Quick question: How long does it typically take {{company}} to collect from "
        "customers? Running an analysis.",
    ),
    (
        "D",
        "Industry-Specific",
        "{{firstName}}, food & beverage distribution - we help companies like "
        "{{company}} accelerate collections.",
    ),
]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist yet. Does NOT seed any account - see create_account()."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_token() -> str:
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# Accounts / auth
# ---------------------------------------------------------------------------

def create_account(company_name: str, login_email: str, password: str) -> dict:
    """Create a new customer account (tenant) and seed its default LinkedIn templates."""
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (company_name, login_email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (company_name, login_email.lower(), password_hash, now_iso()),
        )
        account_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO linkedin_templates (account_id, label, title, body) VALUES (?, ?, ?, ?)",
            [(account_id, label, title, body) for label, title, body in DEFAULT_LINKEDIN_TEMPLATES],
        )
        row = conn.execute("SELECT id, company_name, login_email, created_at FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row)


def get_account_by_email(login_email: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE login_email = ?", (login_email.lower(),)).fetchone()
        return dict(row) if row else None


def set_password(login_email: str, new_password: str) -> bool:
    """Reset an account's password (admin-only - see require_admin_secret in
    auth.py). There is no self-service "forgot password" email flow yet, so
    this is how a forgotten password actually gets fixed for now. Also
    invalidates all of that account's existing sessions, so a reset really
    does lock out whoever had the old password.
    """
    password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE login_email = ?",
            (password_hash, login_email.lower()),
        )
        if cur.rowcount == 0:
            return False
        account_row = conn.execute("SELECT id FROM accounts WHERE login_email = ?", (login_email.lower(),)).fetchone()
        conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_row["id"],))
        return True


def verify_password(login_email: str, password: str):
    """Return the account dict (without password_hash) if credentials are correct, else None."""
    account = get_account_by_email(login_email)
    if not account:
        return None
    if not bcrypt.checkpw(password.encode("utf-8"), account["password_hash"].encode("utf-8")):
        return None
    account = dict(account)
    account.pop("password_hash", None)
    return account


def create_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, account_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, account_id, now_iso(), expires_at),
        )
    return token


def get_account_by_token(token: str):
    """Return the account dict for a valid, non-expired session token, else None."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT a.id, a.company_name, a.login_email, a.created_at, s.expires_at
            FROM sessions s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now_iso():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        return dict(row)


def delete_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


RESET_TOKEN_LIFETIME_HOURS = 1


def create_password_reset_token(account_id: int) -> str:
    """Issue a one-time, short-lived token for the self-service 'forgot
    password' flow. Deliberately short-lived (1 hour) and single-use (see
    consume_password_reset_token) since it's mailed as a plain link."""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_LIFETIME_HOURS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO password_reset_tokens (token, account_id, created_at, expires_at, used) VALUES (?, ?, ?, ?, 0)",
            (token, account_id, now_iso(), expires_at),
        )
    return token


def get_account_for_reset_token(token: str):
    """Return the account dict (without password_hash) for a valid, unused,
    non-expired reset token, else None. Does not consume the token - call
    consume_password_reset_token() once the password has actually been
    changed, so a token that's merely looked up (e.g. loading the reset
    page) doesn't get burned before the user submits the form."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token = ?", (token,)
        ).fetchone()
        if not row or row["used"] or row["expires_at"] < now_iso():
            return None
        account_row = conn.execute(
            "SELECT id, company_name, login_email, created_at FROM accounts WHERE id = ?",
            (row["account_id"],),
        ).fetchone()
        return dict(account_row) if account_row else None


def consume_password_reset_token(token: str):
    with get_conn() as conn:
        conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# Contacts (scoped per account)
# ---------------------------------------------------------------------------

def add_contact(account_id: int, first_name: str, email: str, last_name: str = "", company: str = "", linkedin_url: str = "") -> dict:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO contacts (account_id, first_name, last_name, email, company, linkedin_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, email) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                company=excluded.company,
                linkedin_url=excluded.linkedin_url
            """,
            (account_id, first_name, last_name, email, company, linkedin_url, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? AND email = ?", (account_id, email)
        ).fetchone()
        return dict(row)


def list_contacts(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_contacts(account_id: int) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE account_id = ?", (account_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Campaigns / A-B test (scoped per account)
# ---------------------------------------------------------------------------

def create_campaign(account_id: int, name: str, variants: list) -> dict:
    """
    variants: list of dicts with keys group_label, offer_name, subject_template, body_template.
    Assigns every current contact of this account round-robin across the
    given variants and creates one campaign_recipients row (with its own
    tracking token) per contact. Nothing is sent yet - see launch_campaign().
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (account_id, name, status, created_at) VALUES (?, ?, 'draft', ?)",
            (account_id, name, now_iso()),
        )
        campaign_id = cur.lastrowid

        variant_ids = []
        for v in variants:
            vcur = conn.execute(
                """
                INSERT INTO campaign_variants (campaign_id, group_label, offer_name, subject_template, body_template)
                VALUES (?, ?, ?, ?, ?)
                """,
                (campaign_id, v["group_label"], v["offer_name"], v["subject_template"], v["body_template"]),
            )
            variant_ids.append(vcur.lastrowid)

        contacts = conn.execute(
            "SELECT id FROM contacts WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
        for i, contact in enumerate(contacts):
            variant_id = variant_ids[i % len(variant_ids)] if variant_ids else None
            if variant_id is None:
                continue
            conn.execute(
                """
                INSERT INTO campaign_recipients (campaign_id, variant_id, contact_id, tracking_token)
                VALUES (?, ?, ?, ?)
                """,
                (campaign_id, variant_id, contact["id"], new_token()),
            )

        return get_campaign(campaign_id, account_id, _conn=conn)


def get_campaign(campaign_id: int, account_id: int, _conn=None) -> dict:
    """Returns None if the campaign doesn't exist OR belongs to a different account."""
    def _query(conn):
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id = ? AND account_id = ?", (campaign_id, account_id)
        ).fetchone()
        if not campaign:
            return None
        variants = conn.execute(
            "SELECT * FROM campaign_variants WHERE campaign_id = ?", (campaign_id,)
        ).fetchall()
        return {"campaign": dict(campaign), "variants": [dict(v) for v in variants]}

    if _conn is not None:
        return _query(_conn)
    with get_conn() as conn:
        return _query(conn)


def list_campaigns(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM campaigns WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def campaign_recipients_for_launch(campaign_id: int, account_id: int) -> list:
    """Rows needed to actually send: recipient + variant + contact info. Empty if campaign isn't this account's."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cr.id AS recipient_id, cr.tracking_token,
                   cv.group_label, cv.offer_name, cv.subject_template, cv.body_template,
                   c.id AS contact_id, c.first_name, c.last_name, c.email, c.company
            FROM campaign_recipients cr
            JOIN campaign_variants cv ON cv.id = cr.variant_id
            JOIN contacts c ON c.id = cr.contact_id
            JOIN campaigns camp ON camp.id = cr.campaign_id
            WHERE cr.campaign_id = ? AND camp.account_id = ?
            """,
            (campaign_id, account_id),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_campaign_launched(campaign_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET status = 'launched', launched_at = ? WHERE id = ?",
            (now_iso(), campaign_id),
        )


def record_send_result(recipient_id: int, sent: bool, error: str = None):
    with get_conn() as conn:
        if sent:
            conn.execute(
                "UPDATE campaign_recipients SET sent_at = ?, send_error = NULL WHERE id = ?",
                (now_iso(), recipient_id),
            )
        else:
            conn.execute(
                "UPDATE campaign_recipients SET send_error = ? WHERE id = ?",
                (error, recipient_id),
            )


def get_recipient_by_token(token: str):
    """Public (unauthenticated) lookup used by the /track/* endpoints - not account-scoped by design."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM campaign_recipients WHERE tracking_token = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def record_open(token: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaign_recipients SET opened_at = COALESCE(opened_at, ?) WHERE tracking_token = ?",
            (now_iso(), token),
        )


def record_click(token: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaign_recipients SET clicked_at = COALESCE(clicked_at, ?) WHERE tracking_token = ?",
            (now_iso(), token),
        )


def record_form_fill(token: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaign_recipients SET form_filled_at = COALESCE(form_filled_at, ?) WHERE tracking_token = ?",
            (now_iso(), token),
        )


def campaign_results(campaign_id: int, account_id: int) -> list:
    """Aggregated sent/opens/clicks/fills per variant (group). Empty if campaign isn't this account's."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cv.id AS variant_id, cv.group_label, cv.offer_name,
                   COUNT(cr.id) AS total,
                   SUM(CASE WHEN cr.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN cr.opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opens,
                   SUM(CASE WHEN cr.clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicks,
                   SUM(CASE WHEN cr.form_filled_at IS NOT NULL THEN 1 ELSE 0 END) AS form_fills
            FROM campaign_variants cv
            JOIN campaigns camp ON camp.id = cv.campaign_id
            LEFT JOIN campaign_recipients cr ON cr.variant_id = cv.id
            WHERE cv.campaign_id = ? AND camp.account_id = ?
            GROUP BY cv.id
            ORDER BY cv.group_label
            """,
            (campaign_id, account_id),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# LinkedIn - manual outreach tracker (NOT automation), scoped per account
#
# Automating LinkedIn connection requests or messages violates LinkedIn's
# Terms of Service and risks account suspension, so this backend never talks
# to LinkedIn directly. Instead it gives the "LinkedIn" tab a real, persisted
# log of outreach you did yourself, so the numbers on that tab are actual
# history instead of hardcoded placeholders.
# ---------------------------------------------------------------------------

def list_linkedin_templates(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM linkedin_templates WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_linkedin_template(template_id: int, account_id: int, body: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE linkedin_templates SET body = ? WHERE id = ? AND account_id = ?",
            (body, template_id, account_id),
        )
        row = conn.execute(
            "SELECT * FROM linkedin_templates WHERE id = ? AND account_id = ?", (template_id, account_id)
        ).fetchone()
        return dict(row) if row else None


def log_linkedin_action(account_id: int, contact_name: str, action: str, template_label: str = "", note: str = "", contact_id=None) -> dict:
    """action: one of 'connection_sent', 'connection_accepted', 'message_sent', 'reply_received'."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO linkedin_outreach (account_id, contact_id, contact_name, action, template_label, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, contact_id, contact_name, action, template_label, note, now_iso()),
        )
        row = conn.execute("SELECT * FROM linkedin_outreach WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def list_linkedin_log(account_id: int, limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM linkedin_outreach WHERE account_id = ? ORDER BY created_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def linkedin_stats(account_id: int) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT action, created_at FROM linkedin_outreach WHERE account_id = ?", (account_id,)
        ).fetchall()

    sent_today = 0
    accepted_today = 0
    total_sent = 0
    total_accepted = 0
    messages_sent = 0

    for r in rows:
        is_today = r["created_at"].startswith(today)
        if r["action"] == "connection_sent":
            total_sent += 1
            if is_today:
                sent_today += 1
        elif r["action"] == "connection_accepted":
            total_accepted += 1
            if is_today:
                accepted_today += 1
        elif r["action"] == "message_sent":
            messages_sent += 1

    acceptance_rate = round((total_accepted / total_sent) * 100) if total_sent else 0

    return {
        "connections_sent_today": sent_today,
        "accepted_today": accepted_today,
        "acceptance_rate": acceptance_rate,
        "messages_sent": messages_sent,
        "total_connections_sent": total_sent,
        "total_connections_accepted": total_accepted,
    }
