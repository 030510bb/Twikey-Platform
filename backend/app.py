"""
Twikey Sales Platform - backend.

A FastAPI service that gives the front-end dashboard real, working
functionality behind every tab, for multiple customer accounts:

  - Auth: email+password login, opaque session tokens (see auth.py/database.py).
    Every account only ever sees its own data - see the multi-tenancy note
    in database.py.
  - Email Sync: send/read mail via Gmail API (SEND_AS_EMAIL, shared by
    default). An account can override sending only (not the inbox read) with
    its own SMTP credentials - see the "Email settings" section further down
    and smtp_client.py/crypto.py.
  - Contacts: a small persisted list per account, used by the tabs below.
  - Validation: rule-based outreach message quality checks (see validation.py).
  - A/B Test + Analytics: real campaigns with per-group tracked sends
    (open pixel + click-tracked links), results computed from actual events.
  - LinkedIn: a manual outreach tracker, NOT automation - see the note in
    database.py for why automating LinkedIn actions was deliberately not
    built here (it would violate LinkedIn's Terms of Service).

Run locally with:
    uvicorn app:app --reload --port 8000

See README.md for full setup and DEPLOY.md for cloud hosting.
"""

import csv
import difflib
import hashlib
import hmac
import html
import io
import json
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# Load .env BEFORE importing any of our own modules - auth.py reads
# ADMIN_SECRET from the environment, and gmail_client.py/database.py read
# their own settings the same way. Importing them first and calling
# load_dotenv() after would mean any module-level `os.environ.get(...)` in
# those modules runs before .env has actually been loaded (only matters for
# local dev with a .env file; on Render the real env vars are already set
# before Python even starts, so this ordering doesn't affect production).
load_dotenv()

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, EmailStr

import ai_client
import crypto
import database
import hubspot_client
import imap_client
import prospecting_client
import smtp_client
from auth import get_current_account, get_current_admin, require_admin_secret
from gmail_client import list_recent_messages, send_email, send_html_email
from validation import validate_message

logger = logging.getLogger("twikey_platform")
logging.basicConfig(level=logging.INFO)

SEND_AS_EMAIL = os.environ.get("SEND_AS_EMAIL", "sales@twikeycampaigns.nl")
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",")]
# Optional alternative to CORS_ORIGINS for "this exact domain and every
# subdomain of it" (e.g. each customer getting their own cosmetic subdomain
# like klant.justmeet.tech - see "Eigen domein koppelen" in DEPLOY.md): a
# regex, matched against the full Origin header. Leave unset to keep using
# CORS_ORIGINS as an exact-match list (or "*"). Example that covers
# app.justmeet.tech, api.justmeet.tech, and any-klant.justmeet.tech:
#   CORS_ORIGIN_REGEX=https://([a-zA-Z0-9-]+\.)?justmeet\.tech
CORS_ORIGIN_REGEX = os.environ.get("CORS_ORIGIN_REGEX") or None
BACKEND_PUBLIC_URL = os.environ.get("BACKEND_PUBLIC_URL", "https://twikey-platform-backend.onrender.com")
FRONTEND_PUBLIC_URL = os.environ.get("FRONTEND_PUBLIC_URL", "https://twikey-platform-frontend.onrender.com")

# 1x1 transparent PNG, served by the open-tracking endpoint.
TRACKING_PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844440000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "00057c02b1000000004945454e44ae426082".replace(" ", "")
)

DEFAULT_VARIANTS = [
    {
        "group_label": "A",
        "offer_name": "DSO Analysis",
        "subject_template": "{{firstName}}, snelle vraag over jullie DSO",
        "body_template": (
            "Hi {{firstName}},<br><br>"
            "Free Days Sales Outstanding Analysis - zie je collection benchmark t.o.v. peers.<br><br>"
            "Benieuwd hoe {{company}} scoort?"
        ),
    },
    {
        "group_label": "B",
        "offer_name": "Cash Flow Report",
        "subject_template": "{{firstName}}, hoeveel kapitaal zit vast bij {{company}}?",
        "body_template": (
            "Hi {{firstName}},<br><br>"
            "Free Cash Flow Impact Analysis - zie hoeveel kapitaal er vastzit in openstaande facturen.<br><br>"
            "Interesse in het rapport voor {{company}}?"
        ),
    },
    {
        "group_label": "C",
        "offer_name": "Efficiency Audit",
        "subject_template": "60-seconden audit voor {{company}}",
        "body_template": (
            "Hi {{firstName}},<br><br>"
            "Free Payment Collection Efficiency Score - een 60 seconden audit.<br><br>"
            "Wil je weten waar {{company}} staat?"
        ),
    },
    {
        "group_label": "D",
        "offer_name": "SEPA Readiness",
        "subject_template": "{{firstName}}, klaar voor SEPA Direct Debit?",
        "body_template": (
            "Hi {{firstName}},<br><br>"
            "Free SEPA Direct Debit Readiness Assessment - een 2 minuten check.<br><br>"
            "Zullen we kijken hoe klaar {{company}} is?"
        ),
    },
]

app = FastAPI(title="Twikey Sales Platform - Backend", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Initialised at import time (not only on ASGI startup) so it also runs
# correctly under test runners/tools that call the app without going
# through a full server startup lifecycle.
database.init_db()
database.ensure_seed_account()


@app.on_event("startup")
def _startup():
    database.init_db()
    database.ensure_seed_account()


# ---------------------------------------------------------------------------
# Health (public)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Simple check that the backend is running and which mailbox it uses."""
    return {"status": "ok", "send_as": SEND_AS_EMAIL}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/auth/login")
def api_login(payload: LoginIn):
    account = database.verify_password(payload.email, payload.password)
    if not account:
        raise HTTPException(status_code=401, detail="E-mailadres of wachtwoord onjuist.")
    token = database.create_session(account["user_id"], account["id"])
    return {"token": token, "account": account}


@app.post("/api/auth/logout")
def api_logout(account: dict = Depends(get_current_account), authorization: str = Header(default=None)):
    token = authorization.split(" ", 1)[1].strip()
    database.delete_session(token)
    return {"success": True}


@app.get("/api/auth/me")
def api_me(account: dict = Depends(get_current_account)):
    return {"account": account}


class ForgotPasswordIn(BaseModel):
    email: EmailStr


@app.post("/api/auth/forgot-password")
def api_forgot_password(payload: ForgotPasswordIn):
    """
    Self-service 'forgot password' - request step. Always returns the same
    generic message regardless of whether the e-mail address has an account,
    so this endpoint can't be used to check which e-mail addresses are
    registered (account enumeration). If it *does* match an account, a
    one-time reset link (valid for an hour) is mailed to it via the shared
    Gmail mailbox.
    """
    user = database.get_user_by_email(payload.email)
    if user:
        token = database.create_password_reset_token(user["user_id"])
        reset_link = f"{FRONTEND_PUBLIC_URL}/reset-password.html?token={token}"
        body = (
            f"Hoi,\n\n"
            f"Er is een wachtwoordreset aangevraagd voor je Twikey Sales Platform-account "
            f"({user['email']}).\n\n"
            f"Klik op onderstaande link om een nieuw wachtwoord in te stellen. "
            f"Deze link is 1 uur geldig en werkt maar één keer:\n\n"
            f"{reset_link}\n\n"
            f"Heb je dit niet zelf aangevraagd? Dan kun je deze e-mail negeren - "
            f"er verandert niets aan je account.\n\n"
            f"- Twikey Sales Platform"
        )
        try:
            send_email(SEND_AS_EMAIL, user["email"], "Wachtwoord resetten - Twikey Sales Platform", body)
        except Exception:
            # Don't leak Gmail/service-account errors to an unauthenticated
            # caller, and don't reveal whether the send succeeded - the
            # generic response below covers both cases either way. DO log it
            # server-side though (visible in Render's Logs tab), otherwise a
            # broken Gmail connection here is completely invisible - nobody
            # who can't see the logs would ever know the mail didn't go out.
            logger.exception("forgot-password: failed to send reset e-mail to %s", user["email"])
    return {"message": "Als dit e-mailadres bij ons bekend is, hebben we een resetlink gestuurd."}


class ResetPasswordSelfIn(BaseModel):
    token: str
    new_password: str


@app.post("/api/auth/reset-password")
def api_reset_password_self(payload: ResetPasswordSelfIn):
    """Self-service 'forgot password' - completion step: exchange a valid,
    unused, unexpired token (from the emailed link) for a new password."""
    user = database.get_user_for_reset_token(payload.token)
    if not user:
        raise HTTPException(status_code=400, detail="Deze resetlink is ongeldig, al gebruikt, of verlopen. Vraag een nieuwe aan.")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    database.set_password(user["email"], payload.new_password)
    database.consume_password_reset_token(payload.token)
    return {"success": True}


class CreateAccountIn(BaseModel):
    company_name: str
    login_email: EmailStr
    password: str


@app.post("/api/admin/accounts", dependencies=[Depends(require_admin_secret)])
def api_create_account(payload: CreateAccountIn):
    """
    Bootstrap a new customer account. Gated by the X-Admin-Secret header
    (matching the ADMIN_SECRET env var) instead of being open self-signup -
    see auth.py for why.
    """
    existing = database.get_user_by_email(payload.login_email)
    if existing:
        raise HTTPException(status_code=409, detail="Er bestaat al een account met dit e-mailadres.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    account = database.create_account(payload.company_name, payload.login_email, payload.password)
    return {"success": True, "account": account}


class ResetPasswordIn(BaseModel):
    login_email: EmailStr
    new_password: str


@app.post("/api/admin/accounts/reset-password", dependencies=[Depends(require_admin_secret)])
def api_reset_password(payload: ResetPasswordIn):
    """
    Admin-only password reset. The normal path for a forgotten password is
    now the self-service flow (POST /api/auth/forgot-password + .../reset-
    password, wired up to login.html/forgot-password.html/reset-password.html)
    which mails the account a one-time reset link. This endpoint is the
    fallback for when that isn't an option - e.g. mail delivery is broken,
    or the account's mailbox itself is inaccessible. Also invalidates that
    account's existing sessions.
    """
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    found = database.set_password(payload.login_email, payload.new_password)
    if not found:
        raise HTTPException(status_code=404, detail="Geen account gevonden met dit e-mailadres.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Team (teammates - extra logins that share one account's data)
# ---------------------------------------------------------------------------
#
# An "account" is one customer/tenant; a "user" is one email+password login
# belonging to exactly one account. This section lets an already-logged-in
# user invite/list/remove *other* logins on their OWN account - they all see
# and share the same contacts/campaigns/LinkedIn log (see database.py). This
# is deliberately NOT admin-secret-gated: any teammate can invite/remove
# another, same as e.g. a shared Slack workspace. It's unrelated to
# POST /api/admin/accounts above, which creates a brand new tenant.

class InviteTeammateIn(BaseModel):
    email: EmailStr


@app.post("/api/team/invite")
def api_invite_teammate(payload: InviteTeammateIn, account: dict = Depends(get_current_account)):
    """
    Add a teammate (another login) to the caller's own account. The new
    login gets a random, unknown throwaway password and is immediately
    e-mailed a link (reusing the same reset-password.html page/flow as
    'forgot password') to set their own real password before they can log
    in - nobody, including the person who invited them, ever knows a
    password for someone else's login.
    """
    existing = database.get_user_by_email(payload.email)
    if existing:
        raise HTTPException(status_code=409, detail="Er bestaat al een gebruiker met dit e-mailadres.")
    throwaway_password = secrets.token_urlsafe(24)
    user = database.create_user(account["id"], payload.email, throwaway_password)
    token = database.create_password_reset_token(user["id"])
    setup_link = f"{FRONTEND_PUBLIC_URL}/reset-password.html?token={token}"
    body = (
        f"Hoi,\n\n"
        f"Je bent toegevoegd als teamlid op het Twikey Sales Platform-account van "
        f"{account['company_name']}.\n\n"
        f"Klik op onderstaande link om je eigen wachtwoord in te stellen en in te loggen. "
        f"Deze link is 1 uur geldig en werkt maar één keer:\n\n"
        f"{setup_link}\n\n"
        f"- Twikey Sales Platform"
    )
    try:
        send_email(SEND_AS_EMAIL, payload.email, "Je bent toegevoegd aan Twikey Sales Platform", body)
    except Exception:
        # The teammate row is already created either way - don't fail the
        # whole request just because the welcome e-mail didn't go out, but
        # do log it, otherwise a broken Gmail connection here silently
        # leaves someone unable to ever set a password for their new login.
        logger.exception("team invite: failed to send welcome e-mail to %s", payload.email)
    return {"success": True, "user": {"id": user["id"], "email": user["email"], "created_at": user["created_at"]}}


@app.get("/api/team/users")
def api_list_teammates(account: dict = Depends(get_current_account)):
    """All teammates (logins) on the caller's own account."""
    return {"users": database.list_users(account["id"])}


@app.delete("/api/team/users/{user_id}")
def api_remove_teammate(user_id: int, account: dict = Depends(get_current_account)):
    """
    Remove a teammate's login from the caller's own account. Refuses to
    remove your own login this way (use a settings page for that, not a
    teammate-management one) and refuses to leave an account with zero
    logins (it would become permanently inaccessible).
    """
    if user_id == account["user_id"]:
        raise HTTPException(status_code=400, detail="Je kunt jezelf niet verwijderen als teamlid.")
    if database.count_users(account["id"]) <= 1:
        raise HTTPException(status_code=400, detail="Een account moet minstens één gebruiker hebben.")
    removed = database.delete_user(account["id"], user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Geen teamlid gevonden met dit id op jouw account.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Superadmin / support
#
# A completely separate login space from customer accounts/users (see the
# "admins"/"admin_sessions" tables and get_current_admin in auth.py) - for
# Twikey staff who need to see across every customer account for support
# purposes. Deliberately NOT "log in as a customer" (no impersonation): this
# gives a read-mostly overview (list every account, view one account's
# users/contacts/campaigns/LinkedIn stats) plus the two admin actions support
# actually needs day to day (create an account, reset a teammate's password)
# - all through a real login instead of copy-pasting ADMIN_SECRET curl
# commands.
# ---------------------------------------------------------------------------

class CreateAdminIn(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/superadmin/admins", dependencies=[Depends(require_admin_secret)])
def api_superadmin_create_admin(payload: CreateAdminIn):
    """
    Bootstrap a support/superadmin login. Gated by X-Admin-Secret (same
    shared secret as POST /api/admin/accounts) rather than an admin session,
    since there's no other admin yet the first time this is called. Use this
    once per support person who needs access - after that they log in
    themselves via POST /api/superadmin/login.
    """
    if database.get_admin_by_email(payload.email):
        raise HTTPException(status_code=409, detail="Er bestaat al een beheerder met dit e-mailadres.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    admin = database.create_admin(payload.email, payload.password)
    return {"success": True, "admin": admin}


class AdminLoginIn(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/superadmin/login")
def api_superadmin_login(payload: AdminLoginIn):
    admin = database.verify_admin_password(payload.email, payload.password)
    if not admin:
        raise HTTPException(status_code=401, detail="E-mailadres of wachtwoord onjuist.")
    token = database.create_admin_session(admin["id"])
    return {"token": token, "admin": admin}


@app.post("/api/superadmin/logout")
def api_superadmin_logout(admin: dict = Depends(get_current_admin), authorization: str = Header(default=None)):
    token = authorization.split(" ", 1)[1].strip()
    database.delete_admin_session(token)
    return {"success": True}


@app.get("/api/superadmin/me")
def api_superadmin_me(admin: dict = Depends(get_current_admin)):
    return {"admin": admin}


@app.get("/api/superadmin/accounts")
def api_superadmin_list_accounts(admin: dict = Depends(get_current_admin)):
    """Every customer account with rollup counts (users/contacts/campaigns) - the support overview list."""
    return {"accounts": database.list_accounts_overview()}


@app.post("/api/superadmin/accounts")
def api_superadmin_create_account(payload: CreateAccountIn, admin: dict = Depends(get_current_admin)):
    """Same effect as POST /api/admin/accounts (create a new customer account/tenant), but gated by an
    admin login instead of an X-Admin-Secret header, for use from the support page itself."""
    existing = database.get_user_by_email(payload.login_email)
    if existing:
        raise HTTPException(status_code=409, detail="Er bestaat al een account met dit e-mailadres.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    account = database.create_account(payload.company_name, payload.login_email, payload.password)
    return {"success": True, "account": account}


@app.get("/api/superadmin/accounts/{account_id}")
def api_superadmin_account_detail(account_id: int, admin: dict = Depends(get_current_admin)):
    """Read-mostly support view of one account: its teammates, a capped list of contacts, its
    campaigns, and LinkedIn stats. Not a Gmail-inbox view - email content stays out of this."""
    detail = database.get_account_overview_detail(account_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Geen account gevonden met dit id.")
    return detail


class SuperadminResetPasswordIn(BaseModel):
    new_password: str


@app.post("/api/superadmin/accounts/{account_id}/users/{user_id}/reset-password")
def api_superadmin_reset_user_password(account_id: int, user_id: int, payload: SuperadminResetPasswordIn, admin: dict = Depends(get_current_admin)):
    """Support-initiated password reset for one teammate on one account. Invalidates that user's
    existing sessions, same as the self-service/admin-secret resets elsewhere."""
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    found = database.reset_user_password_for_account(account_id, user_id, payload.new_password)
    if not found:
        raise HTTPException(status_code=404, detail="Geen gebruiker met dit id op dit account.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Email Sync (shared mailbox - see the multi-tenancy note in database.py)
# ---------------------------------------------------------------------------

class SendEmailRequest(BaseModel):
    to: EmailStr
    subject: str
    message: str


def _decrypted_smtp_settings(account_id: int) -> dict | None:
    """The account's own SMTP config with a decrypted, ready-to-use
    password, or None if the account hasn't configured one."""
    row = database.get_smtp_settings(account_id)
    if not row:
        return None
    return {
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "password": crypto.decrypt(row["password_encrypted"]),
        "from_email": row["from_email"],
        "from_name": row["from_name"],
        "use_tls": bool(row["use_tls"]),
    }


def _send_plain_for_account(account_id: int, to: str, subject: str, body: str):
    """Send a plain-text email as this account's own mailbox if it has SMTP
    settings configured, otherwise fall back to the shared SEND_AS_EMAIL
    Gmail sender - same as it worked before this feature existed."""
    custom = _decrypted_smtp_settings(account_id)
    if custom:
        smtp_client.send_email(custom, to, subject, body, subtype="plain")
        return {"id": None}
    return send_email(SEND_AS_EMAIL, to, subject, body)


def _send_html_for_account(account_id: int, to: str, subject: str, html_body: str):
    custom = _decrypted_smtp_settings(account_id)
    if custom:
        smtp_client.send_html_email(custom, to, subject, html_body)
        return
    send_html_email(SEND_AS_EMAIL, to, subject, html_body)


# ---------------------------------------------------------------------------
# Afmeldlink (huisregel, standaard aan - zie accounts.unsubscribe_link_enabled
# in database.py). Geen aparte tabel/kolom voor het token nodig: het is een
# HMAC-SHA256 over het contact-id, geverifieerd bij het afmelden zelf - zelfde
# "publiek, ongeauthenticeerd, maar niet te raden" patroon als de bestaande
# open/click tracking_token. ADMIN_SECRET wordt hier alleen als sleutelmateriaal
# hergebruikt (geen admin-actie), zodat er geen extra environment variable
# nodig is naast wat dit project al vereist.
# ---------------------------------------------------------------------------

_UNSUB_SECRET = (os.environ.get("ADMIN_SECRET") or "twikey-unsubscribe-dev-secret").encode("utf-8")


def _unsubscribe_token(contact_id: int) -> str:
    sig = hmac.new(_UNSUB_SECRET, str(contact_id).encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{contact_id}.{sig}"


def _verify_unsubscribe_token(token: str) -> int | None:
    try:
        contact_id_str, sig = token.split(".", 1)
        contact_id = int(contact_id_str)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_UNSUB_SECRET, contact_id_str.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(expected, sig):
        return None
    return contact_id


def _with_unsubscribe_footer_html(account_id: int, contact_id: int, body_html: str) -> str:
    """Voegt (als de instelling aan staat) een kleine afmeldlink toe onder
    een campagne-mail. Bewust optioneel per account (zie
    accounts.unsubscribe_link_enabled) - niet elk account/land vereist dit,
    en Benjamin wilde expliciet ook zonder kunnen blijven mailen."""
    if not database.get_sending_settings(account_id)["unsubscribe_link_enabled"]:
        return body_html
    url = f"{BACKEND_PUBLIC_URL}/track/unsubscribe/{_unsubscribe_token(contact_id)}"
    return f'{body_html}<br><br><small style="color:#888">Geen mails meer ontvangen? <a href="{url}">Afmelden</a>.</small>'


def _with_unsubscribe_footer_plain(account_id: int, contact_id: int, body: str) -> str:
    if not database.get_sending_settings(account_id)["unsubscribe_link_enabled"]:
        return body
    url = f"{BACKEND_PUBLIC_URL}/track/unsubscribe/{_unsubscribe_token(contact_id)}"
    return f"{body}\n\nGeen mails meer ontvangen? Afmelden: {url}"


@app.post("/api/send")
def api_send_email(payload: SendEmailRequest, account: dict = Depends(get_current_account)):
    """Send an email as this account's own mailbox (if configured via
    POST /api/email-settings) or on behalf of SEND_AS_EMAIL otherwise."""
    try:
        result = _send_plain_for_account(account["id"], payload.to, payload.subject, payload.message)
    except Exception as exc:  # noqa: BLE001 - surface the real reason to the caller
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "message_id": result.get("id")}


@app.get("/api/inbox")
def api_inbox(max_results: int = 10, account: dict = Depends(get_current_account)):
    """Return the most recent inbox messages, unread count and today's count.

    Note: this still only reads the shared Gmail mailbox (SEND_AS_EMAIL),
    even for accounts with their own SMTP sender configured - reading a
    customer's own inbox would need IMAP credentials and consent on top of
    what SMTP sending needs, which is out of scope for now. Their sent mail
    still goes out correctly through their own domain either way."""
    try:
        return list_recent_messages(SEND_AS_EMAIL, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Email settings (per-account SMTP - send campaigns/manual mail through the
# customer's own domain instead of the shared SEND_AS_EMAIL mailbox)
# ---------------------------------------------------------------------------

class EmailSettingsIn(BaseModel):
    host: str
    port: int
    username: str
    password: str = ""  # blank keeps the currently-saved password unchanged
    from_email: EmailStr
    from_name: str = ""
    use_tls: bool = True
    # Optional: only needed for reply-tracking (reading the inbox), not for
    # sending. Blank host disables IMAP for this account.
    imap_host: str = ""
    imap_port: int | None = None
    imap_username: str = ""
    imap_password: str = ""  # blank keeps the currently-saved IMAP password unchanged
    imap_use_ssl: bool = True


class EmailSettingsTestIn(EmailSettingsIn):
    test_to: EmailStr | None = None  # defaults to the logged-in user's own email


@app.get("/api/email-settings")
def api_get_email_settings(account: dict = Depends(get_current_account)):
    """Never returns any password - just enough to show the form pre-filled
    and to tell the dashboard whether this account sends via its own domain
    or the shared Twikey mailbox, and whether IMAP (reply-reading) is set up."""
    row = database.get_smtp_settings(account["id"])
    if not row:
        return {"configured": False, "imap_configured": False}
    return {
        "configured": True,
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "from_email": row["from_email"],
        "from_name": row["from_name"],
        "use_tls": bool(row["use_tls"]),
        "imap_configured": bool(row.get("imap_host")),
        "imap_host": row.get("imap_host") or "",
        "imap_port": row.get("imap_port"),
        "imap_username": row.get("imap_username") or "",
        "imap_use_ssl": bool(row.get("imap_use_ssl", True)),
    }


@app.post("/api/email-settings")
def api_save_email_settings(payload: EmailSettingsIn, account: dict = Depends(get_current_account)):
    existing = database.get_smtp_settings(account["id"])
    if payload.password:
        password_encrypted = crypto.encrypt(payload.password)
    elif existing:
        password_encrypted = existing["password_encrypted"]  # keep it unchanged
    else:
        raise HTTPException(status_code=400, detail="Wachtwoord is verplicht bij het voor het eerst instellen.")

    imap_host = payload.imap_host or None
    imap_password_encrypted = None
    if imap_host:
        if payload.imap_password:
            imap_password_encrypted = crypto.encrypt(payload.imap_password)
        elif existing and existing.get("imap_password_encrypted"):
            imap_password_encrypted = existing["imap_password_encrypted"]
        else:
            raise HTTPException(status_code=400, detail="IMAP-wachtwoord is verplicht als je een IMAP-host invult.")

    database.save_smtp_settings(
        account["id"], payload.host, payload.port, payload.username,
        password_encrypted, payload.from_email, payload.from_name, payload.use_tls,
        imap_host=imap_host, imap_port=payload.imap_port, imap_username=payload.imap_username or None,
        imap_password_encrypted=imap_password_encrypted, imap_use_ssl=payload.imap_use_ssl,
    )
    return {"success": True}


@app.delete("/api/email-settings")
def api_delete_email_settings(account: dict = Depends(get_current_account)):
    """Revert to the shared SEND_AS_EMAIL sender (also removes IMAP)."""
    database.delete_smtp_settings(account["id"])
    return {"success": True}


@app.post("/api/email-settings/test")
def api_test_email_settings(payload: EmailSettingsTestIn, account: dict = Depends(get_current_account)):
    """Sends a real test email with the submitted settings (not necessarily
    saved yet), so a customer can verify their SMTP credentials work before
    committing to them. A blank password reuses the already-saved one, so
    they can re-test without retyping it."""
    settings = {
        "host": payload.host, "port": payload.port, "username": payload.username,
        "password": payload.password, "from_email": payload.from_email,
        "from_name": payload.from_name, "use_tls": payload.use_tls,
    }
    if not settings["password"]:
        existing = database.get_smtp_settings(account["id"])
        if not existing:
            raise HTTPException(status_code=400, detail="Vul een wachtwoord in om te testen.")
        settings["password"] = crypto.decrypt(existing["password_encrypted"])

    to = payload.test_to or account["email"]
    try:
        smtp_client.send_email(
            settings, to,
            "Testmail - Twikey Sales Platform",
            "Dit is een testmail om te controleren of je e-mailinstellingen correct zijn ingesteld. "
            "Als je deze mail ontvangt, werkt het en kun je de instellingen opslaan.",
        )
    except Exception as exc:  # noqa: BLE001 - surface the real SMTP error to the customer
        raise HTTPException(status_code=400, detail=f"Versturen van de testmail is mislukt: {exc}") from exc
    return {"success": True, "sent_to": to}


@app.post("/api/email-settings/test-imap")
def api_test_imap_settings(payload: EmailSettingsTestIn, account: dict = Depends(get_current_account)):
    """Logs in to the submitted IMAP server (not necessarily saved yet) to
    verify the credentials work, without touching any messages."""
    if not payload.imap_host:
        raise HTTPException(status_code=400, detail="Vul een IMAP-host in om te testen.")
    password = payload.imap_password
    if not password:
        existing = database.get_smtp_settings(account["id"])
        if not existing or not existing.get("imap_password_encrypted"):
            raise HTTPException(status_code=400, detail="Vul een IMAP-wachtwoord in om te testen.")
        password = crypto.decrypt(existing["imap_password_encrypted"])

    import imaplib
    port = payload.imap_port or (993 if payload.imap_use_ssl else 143)
    try:
        conn = imaplib.IMAP4_SSL(payload.imap_host, port) if payload.imap_use_ssl else imaplib.IMAP4(payload.imap_host, port)
        try:
            conn.login(payload.imap_username or payload.username, password)
            status, mailboxes = conn.select("INBOX", readonly=True)
            count = int(mailboxes[0]) if status == "OK" and mailboxes and mailboxes[0] else 0
        finally:
            conn.logout()
    except Exception as exc:  # noqa: BLE001 - surface the real IMAP error to the customer
        raise HTTPException(status_code=400, detail=f"Inloggen op IMAP is mislukt: {exc}") from exc
    return {"success": True, "inbox_message_count": count}


# ---------------------------------------------------------------------------
# Verzendinstellingen (Fase 3c, crm-roadmap.md): dagelijkse samenvatting-mail,
# domain warm-up-verzendlimiet, afmeldlink - alle drie account-brede
# aan/uit-schakelaars, hier gebundeld omdat ze in het dashboard ook op één
# kaart staan (Integraties > Verzendinstellingen).
# ---------------------------------------------------------------------------

@app.get("/api/account/sending-settings")
def api_get_sending_settings(account: dict = Depends(get_current_account)):
    return database.get_sending_settings(account["id"])


class SendingSettingsIn(BaseModel):
    daily_digest_enabled: bool | None = None
    daily_send_limit_enabled: bool | None = None
    daily_send_limit: int | None = None
    unsubscribe_link_enabled: bool | None = None


@app.put("/api/account/sending-settings")
def api_update_sending_settings(payload: SendingSettingsIn, account: dict = Depends(get_current_account)):
    if payload.daily_send_limit is not None and payload.daily_send_limit < 1:
        raise HTTPException(status_code=400, detail="De dagelijkse verzendlimiet moet minstens 1 zijn.")
    settings = database.update_sending_settings(
        account["id"],
        daily_digest_enabled=payload.daily_digest_enabled,
        daily_send_limit_enabled=payload.daily_send_limit_enabled,
        daily_send_limit=payload.daily_send_limit,
        unsubscribe_link_enabled=payload.unsubscribe_link_enabled,
    )
    return {"success": True, **settings}


@app.get("/api/dashboard/attention")
def api_dashboard_attention(account: dict = Depends(get_current_account)):
    """"Aandacht nodig"-kaart op het Dashboard-tabblad: mislukte
    verzendingen, openstaande conceptantwoorden, vervallen herinneringen en
    een eventuele verzendwachtrij - zie database.attention_items()."""
    return {"items": database.attention_items(account["id"])}


# Flexible CSV column-name matching: accepts common Dutch and English
# headers for the same field, so a customer doesn't have to rename their
# spreadsheet columns before uploading. Matched case-insensitively.
_CSV_COLUMN_ALIASES = {
    "first_name": {"first_name", "firstname", "voornaam"},
    "last_name": {"last_name", "lastname", "achternaam"},
    "email": {"email", "e-mail", "emailadres", "e-mailadres"},
    "company": {"company", "bedrijf", "bedrijfsnaam", "organisatie"},
    "job_title": {"job_title", "jobtitle", "title", "functie", "functietitel"},
    "sector": {"sector", "branche", "industry"},
    "revenue_range": {"revenue_range", "revenue", "omzet", "jaaromzet", "omzetcategorie", "company_revenue"},
    "linkedin_url": {"linkedin_url", "linkedin", "linkedinurl", "linkedin profiel"},
    "tags": {"tags", "tag", "labels"},
    "domain": {"domain", "domein", "website"},
    "reason": {"reason", "reden"},
}


def _normalize_csv_row(raw_row: dict) -> dict:
    """Maps a raw csv.DictReader row (arbitrary header casing/spelling) onto
    our canonical field names using _CSV_COLUMN_ALIASES. Unrecognised columns
    are dropped; recognised-but-blank columns are simply absent from the result."""
    lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
    result = {}
    for field, aliases in _CSV_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias]:
                result[field] = lowered[alias]
                break
    return result


async def _read_csv_rows(file: UploadFile) -> list:
    """Reads an uploaded CSV file (any of the encodings below) and returns a
    list of normalized dicts - see _normalize_csv_row."""
    raw = await file.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(status_code=400, detail="Kon het CSV-bestand niet lezen (onbekende tekstcodering).")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="Leeg of ongeldig CSV-bestand.")
    return [_normalize_csv_row(row) for row in reader]


async def _read_csv_raw(file: UploadFile) -> tuple:
    """Zoals _read_csv_rows, maar geeft de RUWE headers en rijen terug
    (ongewijzigde kolomkoppen, geen alias-normalisatie) - gebruikt door de
    nieuwe preview/confirm-flow (_suggest_header_mapping hieronder), zodat
    de klant de daadwerkelijke koppen uit zijn bestand ziet en kan corrigeren
    voordat er iets geimporteerd wordt."""
    raw = await file.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(status_code=400, detail="Kon het CSV-bestand niet lezen (onbekende tekstcodering).")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="Leeg of ongeldig CSV-bestand.")
    headers = [h for h in reader.fieldnames if h]
    rows = [{(k or ""): (v or "").strip() for k, v in row.items()} for row in reader]
    return headers, rows


# Fase 3c: intelligente CSV-import (crm-roadmap.md) - "niet meer handmatig
# kolomkoppen hoeven te hernoemen voordat een CSV geimporteerd kan worden".
# Korte, mensvriendelijke omschrijving per canoniek veld - gebruikt in de
# preview-respons (zodat de klant weet wat elk veld betekent) en als context
# voor de AI-verfijning (ai_client.suggest_csv_mapping).
_CSV_FIELD_LABELS = {
    "first_name": "Voornaam",
    "last_name": "Achternaam",
    "email": "E-mailadres (verplicht)",
    "company": "Bedrijfsnaam",
    "job_title": "Functietitel",
    "sector": "Sector / branche",
    "revenue_range": "Omzetcategorie (bv. 1-10M)",
    "linkedin_url": "LinkedIn-profiel-URL",
    "tags": "Tags (komma-gescheiden)",
    "domain": "Bedrijfsdomein / website",
}


def _suggest_header_mapping(headers: list) -> tuple:
    """Deterministische eerste gok voor elke ruwe header: exacte match tegen
    _CSV_COLUMN_ALIASES, anders de dichtstbijzijnde alias (difflib) als die
    voldoende lijkt, anders None. Geeft (mapping, mapping_source) terug -
    mapping_source is 'rules' hier; app.py's preview-endpoint probeert
    daarna optioneel een AI-verfijning en zet dat om naar 'ai' als die iets
    extra's oplevert."""
    alias_to_field = {}
    all_aliases = []
    for field, aliases in _CSV_COLUMN_ALIASES.items():
        for alias in aliases:
            alias_to_field[alias] = field
            all_aliases.append(alias)

    mapping = {}
    for header in headers:
        key = (header or "").strip().lower()
        if key in alias_to_field:
            mapping[header] = alias_to_field[key]
            continue
        close = difflib.get_close_matches(key, all_aliases, n=1, cutoff=0.78)
        mapping[header] = alias_to_field[close[0]] if close else None
    return mapping, "rules"


@app.post("/api/contacts/import-csv/preview")
async def api_import_contacts_csv_preview(
    file: UploadFile = File(...), account: dict = Depends(get_current_account),
):
    """Fase 3c: stap 1 van de intelligente CSV-import. Leest het bestand,
    stelt een kolom-koppeling voor (regels + optioneel een AI-verfijning
    voor kolommen die de regels niet herkenden) en bewaart de ruwe inhoud
    onder een token, zodat POST .../confirm daarna de door de klant
    gecontroleerde/aangepaste koppeling kan toepassen zonder het bestand
    opnieuw te hoeven uploaden."""
    headers, rows = await _read_csv_raw(file)
    mapping, source = _suggest_header_mapping(headers)

    unmapped = [h for h in headers if mapping.get(h) is None]
    if unmapped and ai_client.is_configured():
        try:
            ai_mapping = ai_client.suggest_csv_mapping(headers, rows[:5], _CSV_FIELD_LABELS)
            for h in unmapped:
                if ai_mapping.get(h):
                    mapping[h] = ai_mapping[h]
                    source = "ai"
        except Exception:  # noqa: BLE001 - alleen de alias-matching blijft over, geen harde fout
            logger.exception("csv-import preview: AI-koppeling mislukt, val terug op alleen regels")

    session = database.create_csv_import_session(
        account["id"], file.filename or "import.csv", headers, rows, mapping, source,
    )
    return {
        "success": True,
        "token": session["token"],
        "filename": session["filename"],
        "headers": headers,
        "sample_rows": rows[:5],
        "row_count": len(rows),
        "suggested_mapping": mapping,
        "mapping_source": source,
        "available_fields": _CSV_FIELD_LABELS,
    }


class CsvImportConfirmIn(BaseModel):
    token: str
    mapping: dict[str, str | None]


@app.post("/api/contacts/import-csv/confirm")
def api_import_contacts_csv_confirm(payload: CsvImportConfirmIn, account: dict = Depends(get_current_account)):
    """Fase 3c: stap 2 - past de (door de klant gecontroleerde/aangepaste)
    kolom-koppeling toe op de bij POST .../preview opgeslagen ruwe rijen en
    importeert dan pas echt, met dezelfde add_contact-logica als de directe
    /api/contacts/import-csv. De sessie wordt na gebruik verwijderd (eenmalig
    bruikbaar, net als andere token-gebaseerde flows in dit project)."""
    session = database.get_csv_import_session(payload.token, account["id"])
    if not session:
        raise HTTPException(status_code=404, detail="Import-sessie niet gevonden of al gebruikt. Upload het bestand opnieuw.")

    mapping = payload.mapping
    added, errors = [], []
    for i, raw_row in enumerate(session["rows"], start=1):
        row = {}
        for header, field in mapping.items():
            if not field:
                continue
            value = (raw_row.get(header) or "").strip()
            if value:
                row[field] = value
        email = row.get("email", "")
        if not email or "@" not in email:
            errors.append({"row": i, "error": "Ontbrekend of ongeldig e-mailadres"})
            continue
        contact = database.add_contact(
            account_id=account["id"],
            first_name=row.get("first_name") or email.split("@")[0],
            email=email,
            last_name=row.get("last_name", ""),
            company=row.get("company", ""),
            linkedin_url=row.get("linkedin_url", ""),
            job_title=row.get("job_title", ""),
            sector=row.get("sector", ""),
            revenue_range=row.get("revenue_range", ""),
            source="csv",
        )
        for tag_name in [t.strip() for t in row.get("tags", "").split(",") if t.strip()]:
            database.add_tag_to_contact(contact["id"], account["id"], tag_name)
        _apply_hubspot_exclusion(account["id"], contact)
        added.append(contact)

    database.delete_csv_import_session(payload.token)
    return {"success": True, "added": len(added), "errors": errors, "contacts": added}


# ---------------------------------------------------------------------------
# Contacts / mini-CRM
# ---------------------------------------------------------------------------

class ContactIn(BaseModel):
    first_name: str
    email: EmailStr
    last_name: str = ""
    company: str = ""
    linkedin_url: str = ""
    job_title: str = ""
    sector: str = ""
    revenue_range: str = ""  # Fase 3c: omzetcategorie, o.a. gebruikt voor ICP-scoring


@app.get("/api/contacts")
def api_list_contacts(
    q: str = None, tag: str = None, persona_id: int = None, assigned_to: str = None,
    exclude_excluded: bool = False, exclude_dnc: bool = False,
    account: dict = Depends(get_current_account),
):
    """q searches first/last name, e-mail and company. assigned_to accepts a
    user id, or "none" for unassigned contacts."""
    aid = account["id"]
    resolved_assigned_to = None
    if assigned_to is not None:
        resolved_assigned_to = "none" if assigned_to == "none" else int(assigned_to)
    contacts = database.list_contacts(
        aid, q=q, tag=tag, persona_id=persona_id, assigned_to=resolved_assigned_to,
        exclude_excluded=exclude_excluded, exclude_dnc=exclude_dnc,
    )
    return {"contacts": contacts, "count": database.count_contacts(aid)}


@app.get("/api/contacts/export-csv")
def api_export_contacts_csv(account: dict = Depends(get_current_account)):
    """Volledige export: alle CRM-velden, plus tags en toegewezen teamlid als
    platte kolommen zodat het bestand in Excel/Sheets bruikbaar is. Moet vóór
    GET /api/contacts/{contact_id} geregistreerd staan, anders vangt die
    route "export-csv" op als een (ongeldige) contact_id."""
    contacts = database.list_contacts(account["id"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "first_name", "last_name", "email", "company", "job_title", "sector", "revenue_range", "linkedin_url",
        "tags", "assigned_to", "source", "is_customer", "has_open_quote", "do_not_contact",
        "excluded_reason", "created_at",
    ])
    for c in contacts:
        writer.writerow([
            c["first_name"], c["last_name"], c["email"], c["company"], c.get("job_title", ""),
            c.get("sector", ""), c.get("revenue_range", ""), c["linkedin_url"], ",".join(t["name"] for t in c.get("tags", [])),
            c.get("assigned_to_email") or "", c.get("source", ""), bool(c.get("is_customer")),
            bool(c.get("has_open_quote")), bool(c.get("do_not_contact")), c.get("excluded_reason") or "",
            c["created_at"],
        ])
    buf.seek(0)
    filename = f"contacten-{account['company_name'].replace(' ', '_')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/contacts/{contact_id}")
def api_get_contact(contact_id: int, account: dict = Depends(get_current_account)):
    contact = database.get_contact(contact_id, account["id"])
    if not contact:
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"contact": contact}


@app.get("/api/contacts/{contact_id}/timeline")
def api_contact_timeline(contact_id: int, account: dict = Depends(get_current_account)):
    """Alles wat er met deze lead is gebeurd, nieuwste eerst: CRM-events
    (aangemaakt/tag/toewijzing/uitsluiting) + campagne-mails
    (verzonden/geopend/geklikt/formulier) + LinkedIn-outreach."""
    timeline = database.contact_timeline(contact_id, account["id"])
    if timeline is None:
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"timeline": timeline}


@app.post("/api/contacts")
def api_add_contact(payload: ContactIn, account: dict = Depends(get_current_account)):
    contact = database.add_contact(
        account_id=account["id"],
        first_name=payload.first_name,
        email=payload.email,
        last_name=payload.last_name,
        company=payload.company,
        linkedin_url=payload.linkedin_url,
        job_title=payload.job_title,
        sector=payload.sector,
        revenue_range=payload.revenue_range,
        source="manual",
    )
    _apply_hubspot_exclusion(account["id"], contact)
    return {"success": True, "contact": contact}


class ContactUpdateIn(BaseModel):
    job_title: str | None = None
    sector: str | None = None
    revenue_range: str | None = None
    company: str | None = None
    linkedin_url: str | None = None
    is_customer: bool | None = None
    has_open_quote: bool | None = None
    do_not_contact: bool | None = None


@app.patch("/api/contacts/{contact_id}")
def api_update_contact(contact_id: int, payload: ContactUpdateIn, account: dict = Depends(get_current_account)):
    fields = payload.model_dump(exclude_unset=True)
    contact = database.update_contact(contact_id, account["id"], **fields)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"success": True, "contact": contact}


class BulkContactsIn(BaseModel):
    contacts: list[ContactIn]


@app.post("/api/contacts/bulk")
def api_add_contacts_bulk(payload: BulkContactsIn, account: dict = Depends(get_current_account)):
    added = []
    for c in payload.contacts:
        contact = database.add_contact(
            account_id=account["id"],
            first_name=c.first_name,
            email=c.email,
            last_name=c.last_name,
            company=c.company,
            linkedin_url=c.linkedin_url,
            job_title=c.job_title,
            sector=c.sector,
            revenue_range=c.revenue_range,
            source="manual",
        )
        _apply_hubspot_exclusion(account["id"], contact)
        added.append(contact)
    return {"success": True, "added": len(added), "contacts": added}


@app.post("/api/contacts/import-csv")
async def api_import_contacts_csv(
    file: UploadFile = File(...), source: str = "csv", account: dict = Depends(get_current_account),
):
    """Flexibele CSV-import: herkent NL/EN kolomnamen (zie _CSV_COLUMN_ALIASES).
    Rijen zonder geldig e-mailadres worden overgeslagen en meegeteld als
    fout. Een 'tags'-kolom (komma-gescheiden) koppelt meteen de genoemde
    tags aan het contact. source is ook hoe Vibe Prospecting-imports straks
    (Fase 2) hetzelfde pad hergebruiken met source='vibe_prospecting'."""
    rows = await _read_csv_rows(file)
    added, errors = [], []
    for i, row in enumerate(rows, start=1):
        email = row.get("email", "")
        if not email or "@" not in email:
            errors.append({"row": i, "error": "Ontbrekend of ongeldig e-mailadres"})
            continue
        contact = database.add_contact(
            account_id=account["id"],
            first_name=row.get("first_name") or email.split("@")[0],
            email=email,
            last_name=row.get("last_name", ""),
            company=row.get("company", ""),
            linkedin_url=row.get("linkedin_url", ""),
            job_title=row.get("job_title", ""),
            sector=row.get("sector", ""),
            revenue_range=row.get("revenue_range", ""),
            source=source,
        )
        for tag_name in [t.strip() for t in row.get("tags", "").split(",") if t.strip()]:
            database.add_tag_to_contact(contact["id"], account["id"], tag_name)
        _apply_hubspot_exclusion(account["id"], contact)
        added.append(contact)
    return {"success": True, "added": len(added), "errors": errors, "contacts": added}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@app.get("/api/tags")
def api_list_tags(account: dict = Depends(get_current_account)):
    return {"tags": database.list_tags(account["id"])}


class TagIn(BaseModel):
    name: str


@app.post("/api/tags")
def api_create_tag(payload: TagIn, account: dict = Depends(get_current_account)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Tagnaam mag niet leeg zijn.")
    return {"success": True, "tag": database.get_or_create_tag(account["id"], payload.name)}


@app.delete("/api/tags/{tag_id}")
def api_delete_tag(tag_id: int, account: dict = Depends(get_current_account)):
    if not database.delete_tag(tag_id, account["id"]):
        raise HTTPException(status_code=404, detail="Tag niet gevonden.")
    return {"success": True}


@app.post("/api/contacts/{contact_id}/tags")
def api_add_contact_tag(contact_id: int, payload: TagIn, account: dict = Depends(get_current_account)):
    tag = database.add_tag_to_contact(contact_id, account["id"], payload.name)
    if not tag:
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"success": True, "tag": tag}


@app.delete("/api/contacts/{contact_id}/tags/{tag_id}")
def api_remove_contact_tag(contact_id: int, tag_id: int, account: dict = Depends(get_current_account)):
    if not database.remove_tag_from_contact(contact_id, account["id"], tag_id):
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Buyer personas (Fase 3, crm-roadmap.md) - een beheerde lijst per account.
# Anders dan tags heeft een contact hoogstens één persona tegelijk, zodat de
# koppeling naar "de mail flow voor deze persona" (sequences, campagnes)
# ondubbelzinnig blijft.
# ---------------------------------------------------------------------------

@app.get("/api/buyer-personas")
def api_list_buyer_personas(account: dict = Depends(get_current_account)):
    return {"personas": database.list_buyer_personas(account["id"])}


class BuyerPersonaIn(BaseModel):
    name: str


@app.post("/api/buyer-personas")
def api_create_buyer_persona(payload: BuyerPersonaIn, account: dict = Depends(get_current_account)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Naam mag niet leeg zijn.")
    return {"success": True, "persona": database.create_buyer_persona(account["id"], payload.name)}


@app.delete("/api/buyer-personas/{persona_id}")
def api_delete_buyer_persona(persona_id: int, account: dict = Depends(get_current_account)):
    if not database.delete_buyer_persona(persona_id, account["id"]):
        raise HTTPException(status_code=404, detail="Buyer persona niet gevonden.")
    return {"success": True}


class BuyerPersonaUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None


@app.put("/api/buyer-personas/{persona_id}")
def api_update_buyer_persona(persona_id: int, payload: BuyerPersonaUpdateIn, account: dict = Depends(get_current_account)):
    """Fase 3b: omschrijving (pijnpunten/context) van een persona bewerken -
    gebruikt door het Profiel-tabblad en meegegeven aan Claude bij het
    genereren van variant-suggesties (zie suggest_campaign_variants)."""
    if payload.name is not None and not payload.name.strip():
        raise HTTPException(status_code=400, detail="Naam mag niet leeg zijn.")
    persona = database.update_buyer_persona(persona_id, account["id"], name=payload.name, description=payload.description)
    if not persona:
        raise HTTPException(status_code=404, detail="Buyer persona niet gevonden.")
    return {"success": True, "persona": persona}


class ContactPersonaIn(BaseModel):
    persona_id: int | None = None


@app.put("/api/contacts/{contact_id}/persona")
def api_set_contact_persona(contact_id: int, payload: ContactPersonaIn, account: dict = Depends(get_current_account)):
    contact = database.set_contact_persona(contact_id, account["id"], payload.persona_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact of buyer persona niet gevonden.")
    return {"success": True, "contact": contact}


# ---------------------------------------------------------------------------
# Bedrijfsprofiel / intake + AI-mailsuggesties (Fase 3b, crm-roadmap.md)
#
# Eén AI-verdiepingsronde (geen doorlopend chatgesprek - expliciete
# scope-keuze) om het intakeformulier scherper te krijgen, en op basis
# daarvan AI-gesuggereerde mail-varianten per (optionele) buyer persona -
# i.p.v. de vaste 4 lead-magnet varianten in DEFAULT_VARIANTS. Zoals overal
# elders in dit project: als er geen ANTHROPIC_API_KEY is ingesteld (of de
# aanroep faalt), valt elke endpoint hieronder terug op een simpel,
# voorspelbaar niet-AI alternatief in plaats van te falen - zie
# ai_client.py's module-docstring voor die conventie.
# ---------------------------------------------------------------------------

_FALLBACK_PROFILE_QUESTIONS = [
    "Welk concreet, meetbaar resultaat behalen klanten gemiddeld (bijv. tijdsbesparing, kostenbesparing, %)?",
    "Wat is het belangrijkste pijnpunt van je doelgroep dat jullie oplossen, in hun eigen woorden?",
    "Wie is de typische beslisser, en waar liggen zij 's nachts wakker van?",
    "Wat maakt jullie aanpak anders dan het alternatief dat prospects nu gebruiken?",
]


@app.get("/api/account-profile")
def api_get_account_profile(account: dict = Depends(get_current_account)):
    return {
        "profile": database.get_account_profile(account["id"]),
        "questions": database.list_profile_questions(account["id"]),
    }


class AccountProfileIn(BaseModel):
    value_proposition: str = ""
    usps: list[str] = []


@app.put("/api/account-profile")
def api_update_account_profile(payload: AccountProfileIn, account: dict = Depends(get_current_account)):
    profile = database.upsert_account_profile(account["id"], payload.value_proposition, payload.usps)
    return {"success": True, "profile": profile}


@app.post("/api/account-profile/generate-questions")
def api_generate_profile_questions(account: dict = Depends(get_current_account)):
    profile = database.get_account_profile(account["id"])
    personas = database.list_buyer_personas(account["id"])
    questions, source = _FALLBACK_PROFILE_QUESTIONS, "template"
    if ai_client.is_configured():
        try:
            questions = ai_client.generate_profile_questions(profile["value_proposition"], profile["usps"], personas)
            source = "ai"
        except Exception as exc:  # noqa: BLE001 - fall back to the static question set
            logger.warning("AI-verdiepingsvragen genereren mislukt voor account %s: %s", account["id"], exc)
    stored = database.replace_pending_profile_questions(account["id"], questions)
    return {"success": True, "source": source, "questions": stored}


class ProfileAnswerIn(BaseModel):
    id: int
    answer: str


class ProfileAnswersIn(BaseModel):
    answers: list[ProfileAnswerIn]


@app.post("/api/account-profile/answer-questions")
def api_answer_profile_questions(payload: ProfileAnswersIn, account: dict = Depends(get_current_account)):
    answers = {a.id: a.answer for a in payload.answers}
    questions = database.answer_profile_questions(account["id"], answers)
    return {"success": True, "questions": questions}


class SuggestVariantsIn(BaseModel):
    persona_id: int | None = None
    count: int = 2


@app.post("/api/campaigns/suggest-variants")
def api_suggest_campaign_variants(payload: SuggestVariantsIn, account: dict = Depends(get_current_account)):
    """Geeft variant-suggesties (offer_name/subject_template/body_template)
    terug om in de A/B Test-variant-editor te tonen - slaat niets op, de
    klant kiest/bewerkt eerst voordat een campagne daadwerkelijk wordt
    aangemaakt met POST /api/campaigns."""
    count = max(1, min(payload.count, 4))
    profile = database.get_account_profile(account["id"])
    persona = None
    if payload.persona_id is not None:
        persona = next(
            (p for p in database.list_buyer_personas(account["id"]) if p["id"] == payload.persona_id), None
        )
        if not persona:
            raise HTTPException(status_code=404, detail="Buyer persona niet gevonden.")

    source = "template"
    variants = None
    if ai_client.is_configured():
        try:
            variants = ai_client.generate_variant_suggestions(
                profile["value_proposition"], profile["usps"], persona, count
            )
            source = "ai"
        except Exception as exc:  # noqa: BLE001 - fall back to the profile-based template below
            logger.warning("AI-variant-suggesties genereren mislukt voor account %s: %s", account["id"], exc)

    if not variants:
        # Niet-AI fallback: vult de vaste template rechtstreeks met de eigen
        # waardepropositie/USP's van het account, zodat de knop altijd iets
        # bruikbaars teruggeeft, ook zonder ANTHROPIC_API_KEY.
        value_prop = profile["value_proposition"] or "wat wij voor jullie kunnen betekenen"
        usp_line = f" {profile['usps'][0]}." if profile["usps"] else ""
        persona_label = f" voor {persona['name']}" if persona else ""
        letters = ["A", "B", "C", "D"]
        variants = [
            {
                "offer_name": f"Aanbod {letters[i]}{persona_label}",
                "subject_template": "{{firstName}}, kort voorstel voor {{company}}" + (f" ({persona['name']})" if persona else ""),
                "body_template": (
                    f"Hi {{{{firstName}}}},<br><br>{value_prop}{usp_line}<br><br>"
                    "Benieuwd of dit ook voor {{company}} interessant is?"
                ),
            }
            for i in range(count)
        ]

    return {"success": True, "source": source, "variants": variants}


# ---------------------------------------------------------------------------
# Toewijzen aan een teamlid
# ---------------------------------------------------------------------------

class AssignIn(BaseModel):
    user_id: int | None = None  # None = niet-toewijzen


@app.post("/api/contacts/{contact_id}/assign")
def api_assign_contact(contact_id: int, payload: AssignIn, account: dict = Depends(get_current_account)):
    contact = database.assign_contact(contact_id, account["id"], payload.user_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact of teamlid niet gevonden binnen dit account.")
    return {"success": True, "contact": contact}


# ---------------------------------------------------------------------------
# Reminders (agenderen)
# ---------------------------------------------------------------------------

class ReminderIn(BaseModel):
    contact_id: int
    remind_at: str  # ISO date/datetime
    note: str = ""


@app.post("/api/reminders")
def api_create_reminder(payload: ReminderIn, account: dict = Depends(get_current_account)):
    reminder = database.create_reminder(
        account["id"], payload.contact_id, payload.remind_at, payload.note, created_by=account.get("user_id")
    )
    if not reminder:
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"success": True, "reminder": reminder}


@app.get("/api/reminders")
def api_list_reminders(due_only: bool = False, include_done: bool = False, account: dict = Depends(get_current_account)):
    return {"reminders": database.list_reminders(account["id"], only_due=due_only, only_open=not include_done)}


@app.post("/api/reminders/{reminder_id}/complete")
def api_complete_reminder(reminder_id: int, account: dict = Depends(get_current_account)):
    if not database.complete_reminder(reminder_id, account["id"]):
        raise HTTPException(status_code=404, detail="Herinnering niet gevonden.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Uitsluitlijst (bestaande klanten / lopende offertes)
# ---------------------------------------------------------------------------

class ExclusionIn(BaseModel):
    domain: str
    company_name: str = ""
    reason: str = ""


@app.get("/api/exclusions")
def api_list_exclusions(account: dict = Depends(get_current_account)):
    return {"exclusions": database.list_exclusion_entries(account["id"])}


@app.post("/api/exclusions")
def api_add_exclusion(payload: ExclusionIn, account: dict = Depends(get_current_account)):
    entry = database.add_exclusion_entry(account["id"], payload.domain, payload.company_name, payload.reason, source="manual")
    return {"success": True, "exclusion": entry}


@app.delete("/api/exclusions/{entry_id}")
def api_delete_exclusion(entry_id: int, account: dict = Depends(get_current_account)):
    if not database.delete_exclusion_entry(entry_id, account["id"]):
        raise HTTPException(status_code=404, detail="Uitsluiting niet gevonden.")
    return {"success": True}


@app.post("/api/exclusions/import-csv")
async def api_import_exclusions_csv(file: UploadFile = File(...), account: dict = Depends(get_current_account)):
    """CSV met minimaal een 'domain' of 'email' kolom (en optioneel 'company'/
    'reason') - te vermijden bedrijven, bv. een export van bestaande klanten
    uit een ander systeem."""
    rows = await _read_csv_rows(file)
    added = database.import_exclusion_csv_rows(account["id"], rows)
    return {"success": True, "added": added}


# ---------------------------------------------------------------------------
# Integraties: Vibe Prospecting / Explorium (credential storage only for nu -
# zie crm-roadmap.md Fase 2 voor de echte zoek-/lookalike-endpoints)
# ---------------------------------------------------------------------------

class ProspectingSettingsIn(BaseModel):
    api_key: str


@app.get("/api/integrations/prospecting")
def api_get_prospecting_settings(account: dict = Depends(get_current_account)):
    row = database.get_prospecting_settings(account["id"])
    return {"configured": bool(row)}


@app.post("/api/integrations/prospecting")
def api_save_prospecting_settings(payload: ProspectingSettingsIn, account: dict = Depends(get_current_account)):
    if not payload.api_key.strip():
        raise HTTPException(status_code=400, detail="API-key mag niet leeg zijn.")
    database.save_prospecting_settings(account["id"], crypto.encrypt(payload.api_key.strip()))
    return {"success": True}


@app.delete("/api/integrations/prospecting")
def api_delete_prospecting_settings(account: dict = Depends(get_current_account)):
    database.delete_prospecting_settings(account["id"])
    return {"success": True}


# ---------------------------------------------------------------------------
# Integraties: HubSpot (credential storage only voor nu - de live
# klant/deal-uitsluitingscheck volgt in Fase 2, zie crm-roadmap.md)
# ---------------------------------------------------------------------------

class HubspotSettingsIn(BaseModel):
    access_token: str
    exclude_customers: bool = True
    exclude_open_deals: bool = True


@app.get("/api/integrations/hubspot")
def api_get_hubspot_settings(account: dict = Depends(get_current_account)):
    row = database.get_hubspot_settings(account["id"])
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "exclude_customers": bool(row["exclude_customers"]),
        "exclude_open_deals": bool(row["exclude_open_deals"]),
    }


@app.post("/api/integrations/hubspot")
def api_save_hubspot_settings(payload: HubspotSettingsIn, account: dict = Depends(get_current_account)):
    if not payload.access_token.strip():
        raise HTTPException(status_code=400, detail="Access token mag niet leeg zijn.")
    database.save_hubspot_settings(
        account["id"], crypto.encrypt(payload.access_token.strip()),
        payload.exclude_customers, payload.exclude_open_deals,
    )
    return {"success": True}


@app.delete("/api/integrations/hubspot")
def api_delete_hubspot_settings(account: dict = Depends(get_current_account)):
    database.delete_hubspot_settings(account["id"])
    return {"success": True}


def _apply_hubspot_exclusion(account_id: int, contact: dict):
    """Best-effort live HubSpot check - the third uitsluitlijst-mechanisme
    from crm-roadmap.md, running automatically on top of the manual+CSV
    exclusion checks add_contact() already does (see database.py's
    check_exclusion). Never raises: a HubSpot outage or a bad/expired token
    shouldn't block adding a contact, it just means this particular safety
    net didn't fire for this one contact.

    Mutates `contact["excluded_reason"]` in place (in addition to writing
    it to the database) when it excludes - callers pass in the dict
    returned by add_contact() and hand the same dict back to the API
    caller, so without this the HTTP response would show the pre-check
    state even though the database already reflects the exclusion."""
    if contact.get("excluded_reason") or not contact.get("company_domain"):
        return
    settings_row = database.get_hubspot_settings(account_id)
    if not settings_row:
        return
    try:
        token = crypto.decrypt(settings_row["access_token_encrypted"])
        result = hubspot_client.check_domain(
            token, contact["company_domain"],
            check_customer=bool(settings_row["exclude_customers"]),
            check_open_deal=bool(settings_row["exclude_open_deals"]),
        )
        reason = None
        if result.get("is_customer"):
            reason = "hubspot:customer"
        elif result.get("has_open_deal"):
            reason = "hubspot:open_deal"
        if reason and database.set_contact_excluded_reason(contact["id"], account_id, reason):
            contact["excluded_reason"] = reason
    except Exception as exc:  # noqa: BLE001 - see docstring, this is deliberately non-fatal
        logger.warning("HubSpot-uitsluitingscheck mislukt voor account %s: %s", account_id, exc)


class HubspotCheckIn(BaseModel):
    domain: str


@app.post("/api/hubspot/check")
def api_hubspot_check_domain(payload: HubspotCheckIn, account: dict = Depends(get_current_account)):
    """On-demand version of the same check (e.g. a 'nu controleren' button
    in Integraties), independent of adding a contact - this one DOES
    surface an error to the caller, since here the customer is explicitly
    asking for a live result."""
    settings_row = database.get_hubspot_settings(account["id"])
    if not settings_row:
        raise HTTPException(status_code=400, detail="HubSpot is nog niet gekoppeld (zie Integraties).")
    token = crypto.decrypt(settings_row["access_token_encrypted"])
    try:
        result = hubspot_client.check_domain(
            token, payload.domain.lower().strip(),
            check_customer=bool(settings_row["exclude_customers"]),
            check_open_deal=bool(settings_row["exclude_open_deals"]),
        )
    except hubspot_client.HubspotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"result": result}


# ---------------------------------------------------------------------------
# Fase 2: Vibe Prospecting / Explorium - echte zoek-/lookalike-/enrich-
# endpoints tegen het per-account API-key (zie prospecting_client.py).
# ---------------------------------------------------------------------------

def _decrypted_prospecting_key(account_id: int) -> str:
    row = database.get_prospecting_settings(account_id)
    if not row:
        raise HTTPException(
            status_code=400,
            detail="Er is nog geen Vibe Prospecting/Explorium API-key gekoppeld (zie Integraties).",
        )
    return crypto.decrypt(row["api_key_encrypted"])


class BusinessMatchIn(BaseModel):
    name: str | None = None
    domain: str | None = None


class ProspectingBusinessMatchIn(BaseModel):
    businesses: list[BusinessMatchIn]


@app.post("/api/prospecting/businesses/match")
def api_prospecting_match_businesses(payload: ProspectingBusinessMatchIn, account: dict = Depends(get_current_account)):
    api_key = _decrypted_prospecting_key(account["id"])
    try:
        results = prospecting_client.match_businesses(
            api_key, [b.model_dump(exclude_none=True) for b in payload.businesses]
        )
    except prospecting_client.ExploriumError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"businesses": results}


class ProspectingLookalikeIn(BaseModel):
    business_id: str
    size: int = 20


@app.post("/api/prospecting/businesses/lookalikes")
def api_prospecting_lookalikes(payload: ProspectingLookalikeIn, account: dict = Depends(get_current_account)):
    """Lookalikes (crm-roadmap.md punt 3): bedrijven die lijken op een al
    gematchte business_id."""
    api_key = _decrypted_prospecting_key(account["id"])
    try:
        results = prospecting_client.search_lookalike_businesses(api_key, payload.business_id, payload.size)
    except prospecting_client.ExploriumError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"businesses": results}


class ProspectMatchIn(BaseModel):
    business_id: str | None = None
    job_titles: list[str] | None = None
    full_name: str | None = None
    company_name: str | None = None


class ProspectingProspectsIn(BaseModel):
    prospects: list[ProspectMatchIn]


@app.post("/api/prospecting/prospects/match")
def api_prospecting_match_prospects(payload: ProspectingProspectsIn, account: dict = Depends(get_current_account)):
    api_key = _decrypted_prospecting_key(account["id"])
    try:
        results = prospecting_client.match_prospects(
            api_key, [p.model_dump(exclude_none=True) for p in payload.prospects]
        )
    except prospecting_client.ExploriumError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prospects": results}


class ProspectingEnrichIn(BaseModel):
    prospect_ids: list[str]


@app.post("/api/prospecting/prospects/enrich")
def api_prospecting_enrich(payload: ProspectingEnrichIn, account: dict = Depends(get_current_account)):
    api_key = _decrypted_prospecting_key(account["id"])
    try:
        results = prospecting_client.enrich_prospect_contacts(api_key, payload.prospect_ids)
    except prospecting_client.ExploriumError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prospects": results}


class ProspectingImportContactIn(BaseModel):
    first_name: str
    last_name: str = ""
    email: EmailStr
    company: str = ""
    job_title: str = ""
    linkedin_url: str = ""


class ProspectingImportIn(BaseModel):
    contacts: list[ProspectingImportContactIn]


@app.post("/api/prospecting/import")
def api_prospecting_import(payload: ProspectingImportIn, account: dict = Depends(get_current_account)):
    """Neemt Explorium-resultaten (na match+enrich hierboven, door de
    gebruiker bekeken/bevestigd) over als CRM-contacten - zelfde
    add_contact-pad als CSV-import, met source='vibe_prospecting', inclusief
    de bestaande manual+CSV-uitsluitingscheck plus (indien gekoppeld) de
    live HubSpot-check."""
    added = []
    for c in payload.contacts:
        contact = database.add_contact(
            account_id=account["id"], first_name=c.first_name, email=c.email, last_name=c.last_name,
            company=c.company, linkedin_url=c.linkedin_url, job_title=c.job_title, source="vibe_prospecting",
        )
        _apply_hubspot_exclusion(account["id"], contact)
        added.append(contact)
    return {"success": True, "added": len(added), "contacts": added}


# ---------------------------------------------------------------------------
# Fase 2: bezwaren-bibliotheek (objection_templates)
# ---------------------------------------------------------------------------

class ObjectionTemplateIn(BaseModel):
    category: str
    keywords: str = ""
    suggested_reply: str


@app.get("/api/objections")
def api_list_objections(account: dict = Depends(get_current_account)):
    return {"objections": database.list_objection_templates(account["id"])}


@app.post("/api/objections")
def api_create_objection(payload: ObjectionTemplateIn, account: dict = Depends(get_current_account)):
    return {
        "success": True,
        "objection": database.create_objection_template(
            account["id"], payload.category, payload.keywords, payload.suggested_reply
        ),
    }


@app.put("/api/objections/{template_id}")
def api_update_objection(template_id: int, payload: ObjectionTemplateIn, account: dict = Depends(get_current_account)):
    row = database.update_objection_template(
        template_id, account["id"], payload.category, payload.keywords, payload.suggested_reply
    )
    if not row:
        raise HTTPException(status_code=404, detail="Bezwaar-categorie niet gevonden.")
    return {"success": True, "objection": row}


@app.delete("/api/objections/{template_id}")
def api_delete_objection(template_id: int, account: dict = Depends(get_current_account)):
    if not database.delete_objection_template(template_id, account["id"]):
        raise HTTPException(status_code=404, detail="Bezwaar-categorie niet gevonden.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Fase 2: reply-tracking (IMAP) + AI-conceptantwoorden + goedkeuringsscherm.
# Nooit automatisch verzonden tenzij accounts.auto_reply_enabled AAN staat
# (standaard uit - crm-roadmap.md punt 6).
# ---------------------------------------------------------------------------

def _categorize_reply(objection_templates: list, text: str) -> tuple[str, str]:
    """Simpele keyword-matching classifier: geeft (category, suggested_reply)
    terug voor de eerste objection_templates-rij waarvan een van de
    komma-gescheiden keywords in de reply-tekst voorkomt (case-insensitive),
    of ("", "") als niets matcht. Bewust geen ML - voorspelbaar, uitlegbaar,
    en werkt met nul externe afhankelijkheden zelfs zonder Anthropic-key."""
    lowered = (text or "").lower()
    for tpl in objection_templates:
        keywords = [k.strip().lower() for k in (tpl.get("keywords") or "").split(",") if k.strip()]
        if any(kw in lowered for kw in keywords):
            return tpl["category"], tpl["suggested_reply"]
    return "", ""


def _send_reply_draft(account: dict, draft_body: str, reply: dict):
    subject = reply.get("subject") or ""
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    _send_plain_for_account(account["id"], reply["from_email"], reply_subject, draft_body)


@app.post("/api/replies/fetch")
def api_fetch_replies(account: dict = Depends(get_current_account)):
    """Haalt nieuwe berichten op via de gekoppelde IMAP-mailbox (sinds de
    vorige poll - zie smtp_settings.imap_last_uid), slaat elk op als een
    incoming_reply (gematcht aan contact/campagne waar mogelijk),
    categoriseert automatisch tegen de bezwaren-bibliotheek, en maakt een
    conceptantwoord klaar (AI via Claude als ANTHROPIC_API_KEY is ingesteld,
    anders de standaard bezwaar-suggestie) - wordt nooit automatisch
    verstuurd tenzij auto_reply_enabled aanstaat voor dit account."""
    settings_row = database.get_smtp_settings(account["id"])
    if not settings_row or not settings_row.get("imap_host"):
        raise HTTPException(
            status_code=400,
            detail="Er is nog geen IMAP-mailbox gekoppeld (zie Integraties > Mail-instellingen).",
        )

    imap_settings = {
        "imap_host": settings_row["imap_host"],
        "imap_port": settings_row.get("imap_port"),
        "imap_username": settings_row.get("imap_username") or settings_row["username"],
        "imap_password": crypto.decrypt(settings_row["imap_password_encrypted"]),
        "imap_use_ssl": bool(settings_row.get("imap_use_ssl", True)),
    }
    try:
        messages = imap_client.fetch_new_messages(
            imap_settings, since_uid=database.get_imap_last_uid(account["id"])
        )
    except Exception as exc:  # noqa: BLE001 - surface the real IMAP error to the customer
        raise HTTPException(status_code=400, detail=f"Ophalen via IMAP is mislukt: {exc}") from exc

    objection_templates = database.list_objection_templates(account["id"])
    auto_enabled = database.get_auto_reply_enabled(account["id"])
    highest_uid = database.get_imap_last_uid(account["id"])
    new_replies, new_drafts = [], []

    for msg in messages:
        highest_uid = max(highest_uid, msg["uid"])
        category, suggested = _categorize_reply(objection_templates, msg["body"])
        stored = database.record_incoming_reply(
            account["id"], msg["from_email"], msg["subject"], msg["body"],
            str(msg["uid"]), msg["received_at"], objection_category=category,
        )
        if not stored:
            continue  # already processed - keeps this endpoint safe to call repeatedly
        reply = database.get_incoming_reply(stored["id"], account["id"])
        new_replies.append(reply)

        draft_body, source = suggested, "template"
        if ai_client.is_configured():
            try:
                contact_name = f"{reply.get('first_name') or ''} {reply.get('last_name') or ''}".strip()
                draft_body = ai_client.draft_reply(
                    msg["body"], category, suggested, contact_name, reply.get("company") or ""
                )
                source = "ai"
            except Exception as exc:  # noqa: BLE001 - fall back to the template suggestion
                logger.warning("AI-conceptantwoord genereren mislukt voor reply %s: %s", reply["id"], exc)
        if not draft_body:
            draft_body = (
                "Bedankt voor je reactie - ik kijk hier persoonlijk naar en kom snel bij je terug."
            )

        draft = database.create_reply_draft(account["id"], reply["id"], draft_body, source=source)
        if auto_enabled and reply.get("contact_id"):
            try:
                _send_reply_draft(account, draft["draft_body"], reply)
                database.update_reply_draft(draft["id"], account["id"], status="sent")
            except Exception as exc:  # noqa: BLE001
                database.update_reply_draft(draft["id"], account["id"], status="pending", sent_error=str(exc))
        new_drafts.append(draft)

    if highest_uid > database.get_imap_last_uid(account["id"]):
        database.update_imap_last_uid(account["id"], highest_uid)

    return {"success": True, "new_replies": len(new_replies), "new_drafts": len(new_drafts)}


@app.get("/api/replies")
def api_list_replies(account: dict = Depends(get_current_account)):
    return {"replies": database.list_incoming_replies(account["id"])}


@app.get("/api/replies/drafts")
def api_list_reply_drafts(status: str = None, account: dict = Depends(get_current_account)):
    return {"drafts": database.list_reply_drafts(account["id"], status=status)}


class ReplyDraftEditIn(BaseModel):
    draft_body: str | None = None


@app.post("/api/replies/drafts/{draft_id}/approve")
def api_approve_reply_draft(draft_id: int, payload: ReplyDraftEditIn, account: dict = Depends(get_current_account)):
    """De menselijke goedkeuring uit crm-roadmap.md punt 6: de gebruiker mag
    de tekst nog aanpassen voordat 'm daadwerkelijk verstuurd wordt, via
    dezelfde verzendweg (eigen SMTP of de gedeelde Twikey-mailbox) als de
    rest van het platform."""
    draft = database.get_reply_draft(draft_id, account["id"])
    if not draft:
        raise HTTPException(status_code=404, detail="Concept niet gevonden.")
    if draft["status"] != "pending":
        raise HTTPException(status_code=400, detail="Dit concept is al verwerkt.")
    body = payload.draft_body if payload.draft_body is not None else draft["draft_body"]
    try:
        _send_reply_draft(account, body, draft)
    except Exception as exc:  # noqa: BLE001 - surface the real send error to the customer
        database.update_reply_draft(draft_id, account["id"], status="pending", draft_body=body, sent_error=str(exc))
        raise HTTPException(status_code=400, detail=f"Versturen is mislukt: {exc}") from exc
    database.update_reply_draft(
        draft_id, account["id"], status="sent", draft_body=body, reviewed_by=account["user_id"]
    )
    return {"success": True}


@app.post("/api/replies/drafts/{draft_id}/dismiss")
def api_dismiss_reply_draft(draft_id: int, account: dict = Depends(get_current_account)):
    draft = database.get_reply_draft(draft_id, account["id"])
    if not draft:
        raise HTTPException(status_code=404, detail="Concept niet gevonden.")
    database.update_reply_draft(draft_id, account["id"], status="dismissed", reviewed_by=account["user_id"])
    return {"success": True}


class AutoReplyIn(BaseModel):
    enabled: bool


@app.get("/api/settings/auto-reply")
def api_get_auto_reply(account: dict = Depends(get_current_account)):
    return {"enabled": database.get_auto_reply_enabled(account["id"])}


@app.post("/api/settings/auto-reply")
def api_set_auto_reply(payload: AutoReplyIn, account: dict = Depends(get_current_account)):
    """Standaard UIT (crm-roadmap.md punt 6) - dit endpoint is de enige
    manier om het aan te zetten, altijd een expliciete keuze van de klant."""
    database.set_auto_reply_enabled(account["id"], payload.enabled)
    return {"success": True, "enabled": payload.enabled}


# ---------------------------------------------------------------------------
# Fase 2: opvolgmail-sequenties (drip campaigns)
# ---------------------------------------------------------------------------

class SequenceStepIn(BaseModel):
    wait_days: int = 3
    subject_template: str
    body_template: str


class SequenceIn(BaseModel):
    name: str
    steps: list[SequenceStepIn]
    persona_id: int | None = None


@app.get("/api/sequences")
def api_list_sequences(account: dict = Depends(get_current_account)):
    return {"sequences": database.list_sequences(account["id"])}


@app.post("/api/sequences")
def api_create_sequence(payload: SequenceIn, account: dict = Depends(get_current_account)):
    if not payload.steps:
        raise HTTPException(status_code=400, detail="Een sequence heeft minstens 1 stap nodig.")
    steps = [s.model_dump() for s in payload.steps]
    sequence = database.create_sequence(account["id"], payload.name, steps, persona_id=payload.persona_id)
    return {"success": True, "sequence": sequence}


@app.get("/api/sequences/{sequence_id}")
def api_get_sequence(sequence_id: int, account: dict = Depends(get_current_account)):
    seq = database.get_sequence(sequence_id, account["id"])
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence niet gevonden.")
    return seq


class SequenceStatusIn(BaseModel):
    status: str  # "active" | "paused"


@app.post("/api/sequences/{sequence_id}/status")
def api_set_sequence_status(sequence_id: int, payload: SequenceStatusIn, account: dict = Depends(get_current_account)):
    if payload.status not in ("active", "paused"):
        raise HTTPException(status_code=400, detail="Status moet 'active' of 'paused' zijn.")
    if not database.set_sequence_status(sequence_id, account["id"], payload.status):
        raise HTTPException(status_code=404, detail="Sequence niet gevonden.")
    return {"success": True}


class EnrollIn(BaseModel):
    contact_ids: list[int]


@app.post("/api/sequences/{sequence_id}/enroll")
def api_enroll_sequence(sequence_id: int, payload: EnrollIn, account: dict = Depends(get_current_account)):
    enrolled = 0
    for contact_id in payload.contact_ids:
        if database.enroll_contact(sequence_id, account["id"], contact_id):
            enrolled += 1
    return {"success": True, "enrolled": enrolled}


@app.get("/api/sequences/{sequence_id}/enrollments")
def api_list_enrollments(sequence_id: int, account: dict = Depends(get_current_account)):
    return {"enrollments": database.list_enrollments(sequence_id, account["id"])}


@app.post("/api/sequences/auto-enroll-by-persona")
def api_auto_enroll_by_persona(account: dict = Depends(get_current_account)):
    """Fase 3: schrijft in één keer elk contact met een buyer persona (dat nog
    nergens actief loopt) in op de actieve sequence die aan diezelfde persona
    gekoppeld is. Zie database.auto_enroll_by_persona()."""
    return {"success": True, **database.auto_enroll_by_persona(account["id"])}


@app.post("/api/cron/process-sequences", dependencies=[Depends(require_admin_secret)])
def api_process_sequences():
    """Verstuurt elke vervallen sequence-stap, over ALLE accounts heen - dus
    beveiligd met dezelfde X-Admin-Secret als de andere cross-account
    beheer-endpoints, niet met een account-sessie. Bedoeld om periodiek
    aangeroepen te worden (bv. een uur-cron op Render of een externe
    scheduler) - zie DEPLOY.md."""
    processed, skipped, errors, throttled = 0, 0, 0, 0
    for enrollment in database.due_enrollments():
        account_id = enrollment["seq_account_id"]
        if enrollment["do_not_contact"] or enrollment["excluded_reason"]:
            database.skip_enrollment(enrollment["id"], "Contact is niet meer te benaderen of uitgesloten.")
            skipped += 1
            continue
        remaining_budget = database.remaining_daily_budget(account_id)
        if remaining_budget is not None and remaining_budget <= 0:
            # Fase 3c: domain warm-up-verzendlimiet bereikt voor dit account
            # vandaag - deze enrollment blijft gewoon "due" (geen
            # skip_enrollment, dat zou 'm permanent stoppen) en wordt bij de
            # eerstvolgende cron-run vanzelf weer opgepakt, zodra het
            # dagbudget (morgen, of na verhoging) weer ruimte heeft.
            throttled += 1
            continue
        sequence = database.get_sequence(enrollment["sequence_id"], account_id)
        step = next((s for s in sequence["steps"] if s["step_order"] == enrollment["current_step"]), None) if sequence else None
        if not step:
            database.skip_enrollment(enrollment["id"], "Sequence-stap niet gevonden.")
            skipped += 1
            continue
        contact = {
            "first_name": enrollment["first_name"], "last_name": enrollment["last_name"],
            "company": enrollment["company"],
        }
        subject = _render_template(step["subject_template"], contact)
        body = _render_template(step["body_template"], contact)
        body = _with_unsubscribe_footer_plain(account_id, enrollment["contact_id"], body)
        try:
            _send_plain_for_account(account_id, enrollment["email"], subject, body)
            database.record_sequence_send(
                enrollment["id"], step["id"], sent=True, rendered_subject=subject, rendered_body=body,
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            database.record_sequence_send(
                enrollment["id"], step["id"], sent=False, error=str(exc),
                rendered_subject=subject, rendered_body=body,
            )
            errors += 1
    return {"success": True, "processed": processed, "skipped": skipped, "errors": errors, "throttled": throttled}


@app.post("/api/cron/process-campaign-queue", dependencies=[Depends(require_admin_secret)])
def api_process_campaign_queue():
    """Fase 3c: werkt de wachtrij weg die ontstaat wanneer een campagne-
    launch werd afgekapt door de dagelijkse verzendlimiet (domain warm-up,
    zie remaining_daily_budget() in database.py en api_launch_campaign
    hierboven) - per account tot maximaal het resterende dagbudget, oudste
    campagne/ontvanger eerst. Zelfde beveiliging/aanroeppatroon als
    POST /api/cron/process-sequences: bedoeld om periodiek (bv. elk uur)
    van buitenaf getriggerd te worden, zie DEPLOY.md."""
    sent, failed, throttled_accounts = 0, 0, 0
    for account_id in database.account_ids_with_pending_campaign_sends():
        remaining_budget = database.remaining_daily_budget(account_id)
        if remaining_budget is not None and remaining_budget <= 0:
            throttled_accounts += 1
            continue
        take = remaining_budget if remaining_budget is not None else 1000
        for r in database.pending_campaign_recipients_for_account(account_id, take):
            if _attempt_send_campaign_recipient(account_id, r):
                sent += 1
            else:
                failed += 1
    return {"success": True, "sent": sent, "failed": failed, "throttled_accounts": throttled_accounts}


@app.post("/api/cron/process-digests", dependencies=[Depends(require_admin_secret)])
def api_process_digests():
    """Fase 3c: dagelijkse samenvatting-mail (crm-roadmap.md) - stuurt
    hooguit één keer per dag (UTC) per account een overzicht van de
    afgelopen 24 uur (verstuurd/opens/clicks/replies/actieve campagnes en
    sequenties) naar ALLE teamleden op dat account (Benjamins expliciete
    keuze). Verstuurd via het eigen verzendpad van het account (eigen SMTP
    indien ingesteld, anders de gedeelde Twikey-afzender) - net als
    campagnes/opvolgmails: het is een rapportage OVER het eigen account, dus
    een mail vanaf het eigen domein naar het eigen team voelt logischer dan
    vanaf het gedeelde platform-adres. Idempotent: accounts_needing_digest()
    slaat een account over zodra
    last_digest_sent_date vandaag al is, dus vaker draaien dan nodig is
    onschadelijk (zie DEPLOY.md voor hetzelfde cron-patroon als
    process-sequences)."""
    sent, errors = 0, 0
    for account in database.accounts_needing_digest():
        stats = database.digest_stats(account["id"])
        users = database.list_users(account["id"])
        subject = f"Dagelijkse samenvatting - {account['company_name']}"
        body = (
            f"Hoi,\n\nHier is de samenvatting van de afgelopen 24 uur voor {account['company_name']}:\n\n"
            f"- Mails verstuurd: {stats['emails_sent']}\n"
            f"- Geopend: {stats['opens']}\n"
            f"- Geklikt: {stats['clicks']}\n"
            f"- Nieuwe replies: {stats['new_replies']}\n"
            f"- Actieve campagnes: {stats['active_campaigns']}\n"
            f"- Actieve opvolgsequenties: {stats['active_sequences']}\n\n"
            f"Log in op het dashboard voor de volledige details.\n\n"
            f"Deze mail uitzetten kan bij Integraties > Verzendinstellingen.\n\n"
            f"- Twikey Sales Platform"
        )
        account_ok = True
        for user in users:
            try:
                _send_plain_for_account(account["id"], user["email"], subject, body)
            except Exception:  # noqa: BLE001 - een mislukte digest-mail mag de andere teamleden/accounts niet blokkeren
                logger.exception("digest-mail: versturen naar %s mislukt", user["email"])
                account_ok = False
                errors += 1
        database.mark_digest_sent(account["id"])
        if account_ok:
            sent += 1
    return {"success": True, "accounts_sent": sent, "errors": errors}


# ---------------------------------------------------------------------------
# Support: FAQ / kennisbank + supportvragen
# ---------------------------------------------------------------------------

@app.get("/api/support/kb")
def api_list_kb_articles(q: str = None):
    """Geen inlog vereist zodat de kennisbank ook vanaf de inlogpagina/
    marketingsite doorzocht kan worden."""
    return {"articles": database.list_kb_articles(q)}


class SupportTicketIn(BaseModel):
    subject: str
    message: str


@app.post("/api/support/tickets")
def api_create_support_ticket(payload: SupportTicketIn, account: dict = Depends(get_current_account)):
    ticket = database.create_support_ticket(account["id"], account.get("user_id"), payload.subject, payload.message)
    return {"success": True, "ticket": ticket}


@app.get("/api/support/tickets")
def api_list_support_tickets(account: dict = Depends(get_current_account)):
    return {"tickets": database.list_support_tickets(account["id"])}


@app.get("/api/superadmin/support/tickets")
def api_superadmin_list_support_tickets(admin: dict = Depends(get_current_admin)):
    return {"tickets": database.list_all_support_tickets()}


class SupportTicketReplyIn(BaseModel):
    reply: str


@app.post("/api/superadmin/support/tickets/{ticket_id}/reply")
def api_superadmin_reply_support_ticket(ticket_id: int, payload: SupportTicketReplyIn, admin: dict = Depends(get_current_admin)):
    return {"success": True, "ticket": database.reply_support_ticket(ticket_id, payload.reply)}


# ---------------------------------------------------------------------------
# Ideeenbus (crm-roadmap.md): feedback/ideeen van klanten voor de roadmap -
# apart van support_tickets hierboven (dat verwacht een individueel
# antwoord/oplossing; dit is input voor toekomstige ontwikkeling, met een
# status die het team kan bijwerken zodat klanten zien wat ermee gebeurt).
# ---------------------------------------------------------------------------

class FeedbackIn(BaseModel):
    message: str
    category: str = "idee"  # "idee" | "bug" | "vraag"


@app.post("/api/feedback")
def api_create_feedback(payload: FeedbackIn, account: dict = Depends(get_current_account)):
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Vul een omschrijving in.")
    item = database.create_feedback_item(account["id"], account.get("user_id"), payload.category, payload.message.strip())
    return {"success": True, "item": item}


@app.get("/api/feedback")
def api_list_feedback(account: dict = Depends(get_current_account)):
    """Een account ziet alleen zijn eigen ingediende ideeen/feedback, met
    status - net als de supportvragen hierboven."""
    return {"items": database.list_feedback_items(account["id"])}


@app.get("/api/superadmin/feedback")
def api_superadmin_list_feedback(admin: dict = Depends(get_current_admin)):
    return {"items": database.list_all_feedback_items()}


class FeedbackStatusIn(BaseModel):
    status: str  # "nieuw" | "in overweging" | "op de roadmap" | "gebouwd" | "afgewezen"


@app.put("/api/superadmin/feedback/{feedback_id}/status")
def api_superadmin_set_feedback_status(feedback_id: int, payload: FeedbackStatusIn, admin: dict = Depends(get_current_admin)):
    item = database.set_feedback_status(feedback_id, payload.status)
    if not item:
        raise HTTPException(status_code=404, detail="Feedback-item niet gevonden.")
    return {"success": True, "item": item}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidateMessageIn(BaseModel):
    text: str
    platform: str = "linkedin"  # "linkedin" or "email" - affects the character-limit check


@app.post("/api/validate-message")
def api_validate_message(payload: ValidateMessageIn, account: dict = Depends(get_current_account)):
    return validate_message(payload.text, platform=payload.platform)


# ---------------------------------------------------------------------------
# Campaigns / A-B test
# ---------------------------------------------------------------------------

class VariantIn(BaseModel):
    group_label: str
    offer_name: str
    subject_template: str
    body_template: str
    persona_id: int | None = None  # Fase 3: koppelt deze variant aan één buyer persona


class CampaignIn(BaseModel):
    name: str
    variants: list[VariantIn] | None = None  # omit to use the 4 default lead-magnet offers
    include_excluded: bool = False  # override de uitsluitlijst (bestaande klant/lopende offerte)
    persona_id: int | None = None  # Fase 3: stuur deze campagne alleen naar contacten met deze buyer persona


@app.get("/api/campaigns/default-variants")
def api_default_campaign_variants(account: dict = Depends(get_current_account)):
    """De vaste 4 lead-magnet varianten - los opvraagbaar zodat de
    variant-editor in het dashboard ze kan voorladen zonder de tekst hier
    te dupliceren. Platform-brede, niet-klantspecifieke content (net als de
    "4 Lead Magnet Offers"-uitleg in de A/B Test-tab); `account` is hier
    alleen om consistent achter login te blijven, net als elke andere
    endpoint in dit bestand."""
    return {"variants": DEFAULT_VARIANTS}


@app.get("/api/campaigns")
def api_list_campaigns(account: dict = Depends(get_current_account)):
    return {"campaigns": database.list_campaigns(account["id"])}


@app.post("/api/campaigns")
def api_create_campaign(payload: CampaignIn, account: dict = Depends(get_current_account)):
    aid = account["id"]
    if database.count_contacts(aid) == 0:
        raise HTTPException(
            status_code=400,
            detail="Geen contacten aanwezig. Voeg eerst contacten toe via POST /api/contacts voordat je een campagne maakt.",
        )
    variants = [v.model_dump() for v in payload.variants] if payload.variants else DEFAULT_VARIANTS
    result = database.create_campaign(
        aid, payload.name, variants, include_excluded=payload.include_excluded, only_persona_id=payload.persona_id,
    )
    return {"success": True, **result}


def _render_template(template: str, contact: dict) -> str:
    return (
        template.replace("{{firstName}}", contact.get("first_name") or "")
        .replace("{{lastName}}", contact.get("last_name") or "")
        .replace("{{company}}", contact.get("company") or "")
    )


def _attempt_send_campaign_recipient(aid: int, r: dict) -> bool:
    """Rendert en verstuurt één campagne-ontvanger, en logt het resultaat
    (record_send_result) - gedeeld tussen api_launch_campaign (directe
    launch) en api_process_campaign_queue (het wegwerken van een wachtrij
    die is ontstaan doordat de dagelijkse verzendlimiet een launch afkapte,
    zie remaining_daily_budget()). Geeft True terug bij een geslaagde
    verzending."""
    subject = _render_template(r["subject_template"], r)
    body_html = _render_template(r["body_template"], r)

    landing_url = (
        f"{FRONTEND_PUBLIC_URL}/lead-magnet.html"
        f"?token={r['tracking_token']}&offer={urllib.parse.quote(r['offer_name'])}"
    )
    click_url = f"{BACKEND_PUBLIC_URL}/track/click/{r['tracking_token']}?url={urllib.parse.quote(landing_url, safe='')}"
    pixel_url = f"{BACKEND_PUBLIC_URL}/track/open/{r['tracking_token']}.png"

    full_html = (
        f"{body_html}<br><br>"
        f'<a href="{click_url}">Bekijk je gratis {html.escape(r["offer_name"])}</a>'
        f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="">'
    )
    full_html = _with_unsubscribe_footer_html(aid, r["contact_id"], full_html)

    try:
        _send_html_for_account(aid, r["email"], subject, full_html)
        database.record_send_result(
            r["recipient_id"], sent=True, rendered_subject=subject, rendered_body=full_html,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        database.record_send_result(
            r["recipient_id"], sent=False, error=str(exc),
            rendered_subject=subject, rendered_body=full_html,
        )
        return False


@app.post("/api/campaigns/{campaign_id}/launch")
def api_launch_campaign(campaign_id: int, account: dict = Depends(get_current_account)):
    aid = account["id"]
    campaign = database.get_campaign(campaign_id, aid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne niet gevonden")

    recipients = database.campaign_recipients_for_launch(campaign_id, aid)
    if not recipients:
        raise HTTPException(status_code=400, detail="Geen ontvangers voor deze campagne (geen contacten aanwezig toen de campagne werd aangemaakt)")

    # Fase 3c: domain warm-up. Bij een actieve dagelijkse verzendlimiet wordt
    # een launch afgekapt tot wat er vandaag nog mag - de resterende
    # ontvangers blijven onaangeroerd (sent_at/send_error allebei leeg) en
    # vormen zo automatisch een wachtrij die POST /api/cron/process-campaign-
    # queue de komende dagen wegwerkt zodra er weer ruimte is.
    queued = 0
    remaining_budget = database.remaining_daily_budget(aid)
    if remaining_budget is not None and len(recipients) > remaining_budget:
        queued = len(recipients) - remaining_budget
        recipients = recipients[:remaining_budget]

    sent, failed = 0, 0
    for r in recipients:
        if _attempt_send_campaign_recipient(aid, r):
            sent += 1
        else:
            failed += 1

    database.mark_campaign_launched(campaign_id)
    return {"success": True, "sent": sent, "failed": failed, "queued": queued, "total": sent + failed + queued}


@app.get("/api/campaigns/{campaign_id}/results")
def api_campaign_results(campaign_id: int, account: dict = Depends(get_current_account)):
    aid = account["id"]
    campaign = database.get_campaign(campaign_id, aid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne niet gevonden")
    return {"campaign": campaign["campaign"], "results": database.campaign_results(campaign_id, aid)}


@app.get("/api/campaigns/overview")
def api_campaigns_overview(account: dict = Depends(get_current_account)):
    """Fase 3c: één rij per campagne (verstuurd/opens/clicks/replies/
    conversie) - anders dan /api/campaigns/{id}/results (per variant BINNEN
    één campagne), dit is het overzicht over ALLE campagnes van dit account
    heen, zie database.campaigns_overview()."""
    return {"campaigns": database.campaigns_overview(account["id"])}


@app.get("/api/analytics/icp-scores")
def api_icp_scores(account: dict = Depends(get_current_account)):
    """Fase 3: scoort sector/buyer persona/omzetcategorie (los en
    gecombineerd) op basis van bestaande open/click/reply-data, om te zien
    welke combinatie de beste resultaten oplevert ("ideal customer
    profile") - zie database.icp_scores() voor de scoreformule en de
    ICP_MIN_SAMPLE-afkap tegen ruis bij kleine steekproeven."""
    return database.icp_scores(account["id"])


# ---------------------------------------------------------------------------
# Tracking (public endpoints, hit by email clients / the lead-magnet page -
# deliberately NOT behind auth, since anonymous recipients trigger these)
# ---------------------------------------------------------------------------

@app.get("/track/open/{token}.png")
def track_open(token: str):
    database.record_open(token)
    return Response(content=TRACKING_PIXEL, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/track/click/{token}")
def track_click(token: str, url: str):
    database.record_click(token)
    return RedirectResponse(url=url, status_code=302)


@app.get("/track/unsubscribe/{token}")
def track_unsubscribe(token: str):
    """Publieke afmeldlink (zie _unsubscribe_token/_with_unsubscribe_footer_*
    hierboven) - zet do_not_contact op het contact dat bij dit
    HMAC-ondertekende token hoort, ongeacht welk account. Toont een simpele
    bevestigingspagina i.p.v. JSON, aangezien dit door een mens vanuit een
    mailclient wordt geopend."""
    contact_id = _verify_unsubscribe_token(token)
    contact = database.set_do_not_contact_by_id(contact_id) if contact_id else None
    if not contact:
        message = "Deze afmeldlink is niet (meer) geldig."
    else:
        message = "Je bent afgemeld. Je ontvangt geen mails meer van ons."
    return Response(
        content=(
            "<html><body style='font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;color:#18407D'>"
            f"<h2>{html.escape(message)}</h2></body></html>"
        ),
        media_type="text/html",
    )


class FormFillIn(BaseModel):
    name: str = ""
    email: str = ""
    company: str = ""


@app.post("/track/formfill/{token}")
def track_formfill(token: str, payload: FormFillIn):
    recipient = database.get_recipient_by_token(token)
    if not recipient:
        raise HTTPException(status_code=404, detail="Onbekende tracking token")
    database.record_form_fill(token)
    return {"success": True}


# ---------------------------------------------------------------------------
# LinkedIn - manual outreach tracker (see database.py for why this is
# deliberately NOT automated)
# ---------------------------------------------------------------------------

@app.get("/api/linkedin/stats")
def api_linkedin_stats(account: dict = Depends(get_current_account)):
    return database.linkedin_stats(account["id"])


@app.get("/api/linkedin/templates")
def api_linkedin_templates(account: dict = Depends(get_current_account)):
    return {"templates": database.list_linkedin_templates(account["id"])}


class TemplateUpdateIn(BaseModel):
    body: str


@app.put("/api/linkedin/templates/{template_id}")
def api_update_linkedin_template(template_id: int, payload: TemplateUpdateIn, account: dict = Depends(get_current_account)):
    updated = database.update_linkedin_template(template_id, account["id"], payload.body)
    if not updated:
        raise HTTPException(status_code=404, detail="Template niet gevonden")
    return {"success": True, "template": updated}


class LinkedinLogIn(BaseModel):
    contact_name: str
    action: str  # connection_sent | connection_accepted | message_sent | reply_received
    template_label: str = ""
    note: str = ""
    contact_id: int | None = None


VALID_LINKEDIN_ACTIONS = {"connection_sent", "connection_accepted", "message_sent", "reply_received"}


@app.post("/api/linkedin/log")
def api_log_linkedin(payload: LinkedinLogIn, account: dict = Depends(get_current_account)):
    if payload.action not in VALID_LINKEDIN_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action moet een van {sorted(VALID_LINKEDIN_ACTIONS)} zijn")
    entry = database.log_linkedin_action(
        account_id=account["id"],
        contact_name=payload.contact_name,
        action=payload.action,
        template_label=payload.template_label,
        note=payload.note,
        contact_id=payload.contact_id,
    )
    return {"success": True, "entry": entry}


@app.get("/api/linkedin/log")
def api_linkedin_log(limit: int = 50, account: dict = Depends(get_current_account)):
    return {"log": database.list_linkedin_log(account["id"], limit=limit)}
