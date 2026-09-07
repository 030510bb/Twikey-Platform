"""
Postgres (Supabase) storage for the Twikey Sales Platform backend.

This backs every account's Contacts, A/B Test / Analytics and
LinkedIn-tracking data, plus the login system itself (accounts, users +
sessions).

An "account" is one customer/tenant (e.g. Twikey Campaigns); a "user" is one
login (one e-mail + password) belonging to exactly one account. Multiple
users can belong to the same account and share all of that account's data -
that's the "teammates" feature (see create_user/list_users/delete_user)
layered on top of the original one-login-per-account model. Every
tenant-scoped table (contacts, campaigns, linkedin_*) is still keyed by
account_id, not user_id - teammates on the same account share that data by
design, they aren't isolated from each other.

Why Postgres/Supabase (and not SQLite anymore): this used to be a local
SQLite file, which was simple but had a real problem on Render's free plan -
that disk is ephemeral and gets wiped on every deploy, taking every account,
password and contact with it. Supabase's Postgres is a normal always-on
database reachable over the network, so none of that data ever disappears
just because you pushed new code. Set DATABASE_URL (see README.md/DEPLOY.md
for where to find that in your Supabase project) and everything below talks
to it instead.

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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL")

SESSION_LIFETIME_DAYS = 30

# Postgres dialect (this used to be SQLite - see the module docstring for
# why that changed). Differences from the old SQLite version: SERIAL instead
# of INTEGER PRIMARY KEY AUTOINCREMENT for auto-incrementing ids; everything
# else (table/column names, UNIQUE/REFERENCES constraints) is unchanged, so
# no data-shape/behaviour changes ripple into the rest of this file.
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    company_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
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
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    launched_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign_variants (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    group_label TEXT NOT NULL,
    offer_name TEXT NOT NULL,
    subject_template TEXT NOT NULL,
    body_template TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_recipients (
    id SERIAL PRIMARY KEY,
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
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    label TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linkedin_outreach (
    id SERIAL PRIMARY KEY,
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


# ---------------------------------------------------------------------------
# Connection handling
#
# The rest of this file was originally written against sqlite3, where
# conn.execute(sql, params) is a convenience shorthand that creates a cursor,
# runs the query, and returns it - and "?" is the placeholder style. Rather
# than rewrite every single query/call-site for psycopg2 (a much bigger,
# riskier diff), _Conn below reproduces that same shorthand on top of a
# psycopg2 connection, translating "?" -> "%s" and returning dict-like rows
# (via RealDictCursor) so `row["some_column"]` keeps working unchanged.
# ---------------------------------------------------------------------------

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. This backend stores its data in Postgres "
                "(Supabase) now - see the 'Database (Supabase)' section in README.md "
                "for where to find your connection string and how to set it."
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _pool


class _Conn:
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql.replace("?", "%s"), list(seq_of_params))
        return cur

    def executescript(self, sql):
        # No "?" placeholders in SCHEMA, and psycopg2 sends a param-less
        # execute() as a simple query, which Postgres happily runs as
        # multiple ";"-separated statements in one call - same effect as
        # sqlite3's executescript().
        cur = self._conn.cursor()
        cur.execute(sql)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


@contextmanager
def get_conn():
    pool = _get_pool()
    pg_conn = pool.getconn()
    conn = _Conn(pg_conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(pg_conn)


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

def create_account(company_name: str, admin_email: str, admin_password: str) -> dict:
    """Create a new customer account (tenant) with its first user, and seed
    the account's default LinkedIn templates. Additional logins for the same
    account are added afterward with create_user()."""
    password_hash = bcrypt.hashpw(admin_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (company_name, created_at) VALUES (?, ?) RETURNING id",
            (company_name, now_iso()),
        )
        account_id = cur.fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO users (account_id, email, password_hash, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (account_id, admin_email.lower(), password_hash, now_iso()),
        )
        user_id = cur.fetchone()["id"]
        conn.executemany(
            "INSERT INTO linkedin_templates (account_id, label, title, body) VALUES (?, ?, ?, ?)",
            [(account_id, label, title, body) for label, title, body in DEFAULT_LINKEDIN_TEMPLATES],
        )
        account_row = conn.execute("SELECT id, company_name, created_at FROM accounts WHERE id = ?", (account_id,)).fetchone()
        user_row = conn.execute("SELECT id, email, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        result = dict(account_row)
        result["user_id"] = user_row["id"]
        result["email"] = user_row["email"]
        return result


def create_user(account_id: int, email: str, password: str) -> dict:
    """Add another login (teammate) to an existing account. They share every
    bit of that account's data - contacts, campaigns, LinkedIn log - there is
    no per-user isolation within an account, only between accounts."""
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (account_id, email, password_hash, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (account_id, email.lower(), password_hash, now_iso()),
        )
        user_id = cur.fetchone()["id"]
        row = conn.execute("SELECT id, account_id, email, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row)


def list_users(account_id: int):
    """All teammates (users) on one account, oldest first. No password hashes."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, email, created_at FROM users WHERE account_id = ? ORDER BY created_at ASC",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_users(account_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE account_id = ?", (account_id,)).fetchone()
        return row["n"]


def delete_user(account_id: int, user_id: int) -> bool:
    """Remove a teammate from an account. Returns False if no such user
    exists on that account (never touches another account's users). Callers
    are responsible for the "not the last user" and "not yourself" checks -
    see api_remove_teammate in app.py - since those are policy, not storage."""
    with get_conn() as conn:
        # Check the user actually belongs to this account BEFORE deleting
        # anything (so we never touch another account's sessions/tokens).
        # Delete the child rows (sessions, reset tokens) before the users
        # row itself - sessions/password_reset_tokens both have a foreign
        # key on users(id), so deleting the parent first trips a FOREIGN KEY
        # constraint failure whenever that user has an active session or
        # reset token outstanding.
        owned = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND account_id = ?", (user_id, account_id)
        ).fetchone()
        if not owned:
            return False
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ? AND account_id = ?", (user_id, account_id))
        return True


def ensure_seed_account():
    """
    Optionally create a starter account automatically on startup if it's
    missing - handy so a fresh deployment doesn't need the admin bootstrap
    curl before anyone can log in. Now that data lives in Supabase Postgres
    (see the module docstring), this no longer runs on every deploy to
    fight an ephemeral disk - it's just a one-time convenience for the very
    first boot against a brand new, empty database.

    Controlled by three env vars, read directly here (not passed in) so this
    can be called from anywhere without threading them through:
      - SEED_ACCOUNT_EMAIL / SEED_ACCOUNT_PASSWORD: both required, else this
        is a no-op (no seed account without an explicit email+password).
      - SEED_ACCOUNT_COMPANY_NAME: optional, defaults to "Twikey Campaigns".

    Deliberately only creates the account if that e-mail doesn't already
    exist - never resets an existing password, so it's always safe to leave
    these env vars set permanently.
    """
    email = os.environ.get("SEED_ACCOUNT_EMAIL")
    password = os.environ.get("SEED_ACCOUNT_PASSWORD")
    if not email or not password:
        return
    if get_user_by_email(email):
        return
    company_name = os.environ.get("SEED_ACCOUNT_COMPANY_NAME", "Twikey Campaigns")
    create_account(company_name, email, password)


def get_user_by_email(email: str):
    """Return the user row (including account_id and company_name via join), or None."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.password_hash, u.account_id, u.created_at,
                   a.company_name
            FROM users u
            JOIN accounts a ON a.id = u.account_id
            WHERE u.email = ?
            """,
            (email.lower(),),
        ).fetchone()
        return dict(row) if row else None


def set_password(email: str, new_password: str) -> bool:
    """Reset one user's password (used by both the self-service reset-token
    flow and the admin fallback endpoint - see auth.py/app.py). Also
    invalidates that user's existing sessions, so a reset really does lock
    out whoever had the old password."""
    password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?",
            (password_hash, email.lower()),
        )
        if cur.rowcount == 0:
            return False
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (email.lower(),)).fetchone()
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_row["id"],))
        return True


def verify_password(email: str, password: str):
    """Return the identity dict (without password_hash) if credentials are
    correct, else None. The returned dict's `id` key is the ACCOUNT id (for
    backward-compatible tenant-scoping in every existing endpoint) alongside
    `user_id`/`email` identifying the specific person who logged in."""
    user = get_user_by_email(email)
    if not user:
        return None
    if not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
        return None
    return {
        "id": user["account_id"],
        "company_name": user["company_name"],
        "created_at": user["created_at"],
        "user_id": user["user_id"],
        "email": user["email"],
    }


def create_session(user_id: int, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, account_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token, user_id, account_id, now_iso(), expires_at),
        )
    return token


def get_account_by_token(token: str):
    """Return the identity dict for a valid, non-expired session token, else
    None. Shaped like verify_password()'s return value - `id` is the account
    id (tenant-scoping key used throughout app.py), plus `user_id`/`email`
    for the specific logged-in person."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT a.id AS id, a.company_name, a.created_at, s.expires_at,
                   u.id AS user_id, u.email
            FROM sessions s
            JOIN users u ON u.id = s.user_id
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


def create_password_reset_token(user_id: int) -> str:
    """Issue a one-time, short-lived token for the self-service 'forgot
    password' flow. Deliberately short-lived (1 hour) and single-use (see
    consume_password_reset_token) since it's mailed as a plain link."""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_LIFETIME_HOURS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO password_reset_tokens (token, user_id, created_at, expires_at, used) VALUES (?, ?, ?, ?, 0)",
            (token, user_id, now_iso(), expires_at),
        )
    return token


def get_user_for_reset_token(token: str):
    """Return the user dict (without password_hash, including account_id and
    company_name) for a valid, unused, non-expired reset token, else None.
    Does not consume the token - call consume_password_reset_token() once
    the password has actually been changed, so a token that's merely looked
    up (e.g. loading the reset page) doesn't get burned before the user
    submits the form."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token = ?", (token,)
        ).fetchone()
        if not row or row["used"] or row["expires_at"] < now_iso():
            return None
        user_row = conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.account_id, u.created_at, a.company_name
            FROM users u
            JOIN accounts a ON a.id = u.account_id
            WHERE u.id = ?
            """,
            (row["user_id"],),
        ).fetchone()
        return dict(user_row) if user_row else None


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
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE account_id = ?", (account_id,)
        ).fetchone()
        return row["n"]


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
            "INSERT INTO campaigns (account_id, name, status, created_at) VALUES (?, ?, 'draft', ?) RETURNING id",
            (account_id, name, now_iso()),
        )
        campaign_id = cur.fetchone()["id"]

        variant_ids = []
        for v in variants:
            vcur = conn.execute(
                """
                INSERT INTO campaign_variants (campaign_id, group_label, offer_name, subject_template, body_template)
                VALUES (?, ?, ?, ?, ?) RETURNING id
                """,
                (campaign_id, v["group_label"], v["offer_name"], v["subject_template"], v["body_template"]),
            )
            variant_ids.append(vcur.fetchone()["id"])

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
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (account_id, contact_id, contact_name, action, template_label, note, now_iso()),
        )
        new_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM linkedin_outreach WHERE id = ?", (new_id,)).fetchone()
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
