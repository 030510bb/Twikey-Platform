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
import functools
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import re
import secrets
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env BEFORE importing any of our own modules - auth.py reads
# ADMIN_SECRET from the environment, and gmail_client.py/database.py read
# their own settings the same way. Importing them first and calling
# load_dotenv() after would mean any module-level `os.environ.get(...)` in
# those modules runs before .env has actually been loaded (only matters for
# local dev with a .env file; on Render the real env vars are already set
# before Python even starts, so this ordering doesn't affect production).
load_dotenv()

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, EmailStr

import ai_client
import crypto
import database
import hubspot_client
import imap_client
import linkedin_ads_client
import meta_ads_client
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

def require_admin_role(account: dict = Depends(get_current_account)) -> dict:
    """Gate voor flow-architectuur-acties (sequences/campagnes aanmaken,
    bewerken, pauzeren/hervatten, lanceren) en teambeheer - crm-roadmap.md,
    "teamleden: naam, functie en rollen". Een teamlid met rol 'user' ziet
    en gebruikt alle resultaten (contacten, sequence-inschrijving,
    campagne-ontvangers toevoegen, etc.) maar mag flows niet aanpassen.
    Teambeheer zit hier ook achter - zonder die gate zou een 'user' zichzelf
    via de teamledenlijst tot 'admin' kunnen promoveren."""
    if account.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Alleen een Beheerder mag dit aanpassen.")
    return account


class InviteTeammateIn(BaseModel):
    email: EmailStr
    first_name: str = ""
    last_name: str = ""
    job_title: str = ""
    role: str = "user"


@app.post("/api/team/invite")
def api_invite_teammate(payload: InviteTeammateIn, account: dict = Depends(require_admin_role)):
    """
    Add a teammate (another login) to the caller's own account. The new
    login gets a random, unknown throwaway password and is immediately
    e-mailed a link (reusing the same reset-password.html page/flow as
    'forgot password') to set their own real password before they can log
    in - nobody, including the person who invited them, ever knows a
    password for someone else's login.
    """
    if payload.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role moet 'admin' of 'user' zijn.")
    existing = database.get_user_by_email(payload.email)
    if existing:
        raise HTTPException(status_code=409, detail="Er bestaat al een gebruiker met dit e-mailadres.")
    throwaway_password = secrets.token_urlsafe(24)
    user = database.create_user(
        account["id"], payload.email, throwaway_password, role=payload.role,
        first_name=payload.first_name.strip(), last_name=payload.last_name.strip(),
        job_title=payload.job_title.strip(),
    )
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
    return {"success": True, "user": user}


@app.get("/api/team/users")
def api_list_teammates(account: dict = Depends(get_current_account)):
    """All teammates (logins) on the caller's own account - visible to
    every role, only editing is gated (require_admin_role)."""
    return {"users": database.list_users(account["id"])}


class UpdateTeammateIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    job_title: str | None = None
    role: str | None = None


@app.put("/api/team/users/{user_id}")
def api_update_teammate(user_id: int, payload: UpdateTeammateIn, account: dict = Depends(get_current_account)):
    """Naam/functie mag iedereen voor zichzelf aanpassen (persoonlijk
    profiel, geen flow-permissie); een rolwijziging, of het aanpassen van
    iemand anders, vereist Beheerder."""
    editing_self = user_id == account["user_id"]
    if payload.role is not None and account.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Alleen een Beheerder mag een rol wijzigen.")
    if not editing_self and account.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Alleen een Beheerder mag andermans gegevens aanpassen.")
    if payload.role is not None and payload.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role moet 'admin' of 'user' zijn.")
    try:
        user = database.update_user(
            account["id"], user_id, first_name=payload.first_name, last_name=payload.last_name,
            job_title=payload.job_title, role=payload.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not user:
        raise HTTPException(status_code=404, detail="Geen teamlid gevonden met dit id op jouw account.")
    return {"success": True, "user": user}


@app.delete("/api/team/users/{user_id}")
def api_remove_teammate(user_id: int, account: dict = Depends(require_admin_role)):
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


def _decrypted_user_smtp_settings(user_id: int) -> dict | None:
    """Zelfde als _decrypted_smtp_settings hierboven, maar voor het
    persoonlijke afzenderadres van één teamlid (crm-roadmap.md, "eigen
    afzenderadres per teamlid") - None als dat teamlid er geen heeft
    ingesteld."""
    row = database.get_user_smtp_settings(user_id)
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


def _resolve_smtp_settings(account_id: int, sender_user_id: int = None) -> dict | None:
    """Bepaalt welk afzenderadres een verzending gebruikt, in volgorde:
    (1) het persoonlijke adres van sender_user_id, als dat teamlid er een
    heeft ingesteld - voor een automatische sequence-stap/campagne-
    verzending is dat de toegewezen accountmanager van het contact
    (contacts.assigned_to), voor een handmatige actie de ingelogde
    gebruiker zelf; (2) het account-brede adres (smtp_settings); (3) None,
    waarna de caller terugvalt op de gedeelde SEND_AS_EMAIL-afzender."""
    if sender_user_id:
        custom = _decrypted_user_smtp_settings(sender_user_id)
        if custom:
            return custom
    return _decrypted_smtp_settings(account_id)


def _send_plain_for_account(account_id: int, to: str, subject: str, body: str, sender_user_id: int = None):
    """Send a plain-text email as this account's own mailbox (or a specific
    teamlid's personal one, see _resolve_smtp_settings) if configured,
    otherwise fall back to the shared SEND_AS_EMAIL Gmail sender - same as
    it worked before this feature existed."""
    custom = _resolve_smtp_settings(account_id, sender_user_id)
    if custom:
        smtp_client.send_email(custom, to, subject, body, subtype="plain")
        return {"id": None}
    return send_email(SEND_AS_EMAIL, to, subject, body)


def _send_html_for_account(account_id: int, to: str, subject: str, html_body: str, sender_user_id: int = None):
    custom = _resolve_smtp_settings(account_id, sender_user_id)
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


def _with_signature_html(account_id: int, body_html: str) -> str:
    """Plakt (als ingesteld) de account-handtekening onder de mail, vóór een
    eventuele afmeldlink - zie accounts.email_signature, instelbaar bij
    Verzendinstellingen."""
    signature = database.get_sending_settings(account_id)["email_signature"]
    if not signature:
        return body_html
    return f'{body_html}<br><br>{signature.replace(chr(10), "<br>")}'


def _with_signature_plain(account_id: int, body: str) -> str:
    signature = database.get_sending_settings(account_id)["email_signature"]
    if not signature:
        return body
    return f"{body}\n\n{signature}"


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
        result = _send_plain_for_account(
            account["id"], payload.to, payload.subject, payload.message, sender_user_id=account["user_id"],
        )
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
# Persoonlijk afzenderadres per teamlid (crm-roadmap.md, "eigen
# afzenderadres per teamlid") - zelfde vorm als de account-brede
# Mail-instellingen hierboven, maar gekoppeld aan account["user_id"] (het
# ingelogde teamlid zelf) i.p.v. account["id"]. Send-only, geen IMAP - zie
# de tabel-docstring bij user_smtp_settings in database.py.
# ---------------------------------------------------------------------------

class UserEmailSettingsIn(BaseModel):
    host: str
    port: int
    username: str
    password: str = ""  # blank keeps the currently-saved password unchanged
    from_email: EmailStr
    from_name: str = ""
    use_tls: bool = True


class UserEmailSettingsTestIn(UserEmailSettingsIn):
    test_to: EmailStr | None = None  # defaults to the logged-in user's own email


@app.get("/api/user/email-settings")
def api_get_user_email_settings(account: dict = Depends(get_current_account)):
    row = database.get_user_smtp_settings(account["user_id"])
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "from_email": row["from_email"],
        "from_name": row["from_name"],
        "use_tls": bool(row["use_tls"]),
    }


@app.post("/api/user/email-settings")
def api_save_user_email_settings(payload: UserEmailSettingsIn, account: dict = Depends(get_current_account)):
    existing = database.get_user_smtp_settings(account["user_id"])
    if payload.password:
        password_encrypted = crypto.encrypt(payload.password)
    elif existing:
        password_encrypted = existing["password_encrypted"]
    else:
        raise HTTPException(status_code=400, detail="Wachtwoord is verplicht bij het voor het eerst instellen.")
    database.save_user_smtp_settings(
        account["user_id"], payload.host, payload.port, payload.username,
        password_encrypted, payload.from_email, payload.from_name, payload.use_tls,
    )
    return {"success": True}


@app.delete("/api/user/email-settings")
def api_delete_user_email_settings(account: dict = Depends(get_current_account)):
    """Verwijdert het persoonlijke afzenderadres - valt terug op het
    account-brede adres (of de gedeelde afzender)."""
    database.delete_user_smtp_settings(account["user_id"])
    return {"success": True}


@app.post("/api/user/email-settings/test")
def api_test_user_email_settings(payload: UserEmailSettingsTestIn, account: dict = Depends(get_current_account)):
    settings = {
        "host": payload.host, "port": payload.port, "username": payload.username,
        "password": payload.password, "from_email": payload.from_email,
        "from_name": payload.from_name, "use_tls": payload.use_tls,
    }
    if not settings["password"]:
        existing = database.get_user_smtp_settings(account["user_id"])
        if not existing:
            raise HTTPException(status_code=400, detail="Vul een wachtwoord in om te testen.")
        settings["password"] = crypto.decrypt(existing["password_encrypted"])

    to = payload.test_to or account["email"]
    try:
        smtp_client.send_email(
            settings, to,
            "Testmail - Twikey Sales Platform",
            "Dit is een testmail om te controleren of je persoonlijke e-mailinstellingen correct zijn ingesteld. "
            "Als je deze mail ontvangt, werkt het en kun je de instellingen opslaan.",
        )
    except Exception as exc:  # noqa: BLE001 - surface the real SMTP error to the customer
        raise HTTPException(status_code=400, detail=f"Versturen van de testmail is mislukt: {exc}") from exc
    return {"success": True, "sent_to": to}


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
    email_signature: str | None = None


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
        email_signature=payload.email_signature,
    )
    return {"success": True, **settings}


class FlowMonitorSettingsIn(BaseModel):
    flow_monitor_enabled: bool | None = None
    flow_monitor_reply_threshold: float | None = None
    flow_monitor_min_sent: int | None = None


@app.get("/api/account/flow-monitor-settings")
def api_get_flow_monitor_settings(account: dict = Depends(get_current_account)):
    return database.get_flow_monitor_settings(account["id"])


@app.put("/api/account/flow-monitor-settings")
def api_update_flow_monitor_settings(payload: FlowMonitorSettingsIn, account: dict = Depends(get_current_account)):
    if payload.flow_monitor_reply_threshold is not None and not (0 <= payload.flow_monitor_reply_threshold <= 1):
        raise HTTPException(status_code=400, detail="De drempel moet tussen 0 en 1 liggen (bv. 0.02 voor 2%).")
    if payload.flow_monitor_min_sent is not None and payload.flow_monitor_min_sent < 1:
        raise HTTPException(status_code=400, detail="Het minimum aantal verstuurd moet minstens 1 zijn.")
    settings = database.update_flow_monitor_settings(
        account["id"], flow_monitor_enabled=payload.flow_monitor_enabled,
        flow_monitor_reply_threshold=payload.flow_monitor_reply_threshold,
        flow_monitor_min_sent=payload.flow_monitor_min_sent,
    )
    return {"success": True, **settings}


class EnrollmentCooldownSettingsIn(BaseModel):
    enrollment_cooldown_enabled: bool | None = None
    enrollment_cooldown_months: int | None = None


@app.get("/api/account/enrollment-cooldown-settings")
def api_get_enrollment_cooldown_settings(account: dict = Depends(get_current_account)):
    return database.get_enrollment_cooldown_settings(account["id"])


@app.put("/api/account/enrollment-cooldown-settings")
def api_update_enrollment_cooldown_settings(payload: EnrollmentCooldownSettingsIn, account: dict = Depends(get_current_account)):
    if payload.enrollment_cooldown_months is not None and payload.enrollment_cooldown_months < 1:
        raise HTTPException(status_code=400, detail="Het aantal maanden moet minstens 1 zijn.")
    settings = database.update_enrollment_cooldown_settings(
        account["id"], enrollment_cooldown_enabled=payload.enrollment_cooldown_enabled,
        enrollment_cooldown_months=payload.enrollment_cooldown_months,
    )
    return {"success": True, **settings}


class SendScheduleSettingsIn(BaseModel):
    send_days: list[int] | None = None
    send_exclude_holidays_nl: bool | None = None


@app.get("/api/account/send-schedule-settings")
def api_get_send_schedule_settings(account: dict = Depends(get_current_account)):
    settings = database.get_send_schedule_settings(account["id"])
    return {**settings, "send_days": [int(d) for d in settings["send_days"].split(",") if d]}


@app.put("/api/account/send-schedule-settings")
def api_update_send_schedule_settings(payload: SendScheduleSettingsIn, account: dict = Depends(get_current_account)):
    send_days_str = None
    if payload.send_days is not None:
        if not payload.send_days or any(not (1 <= d <= 7) for d in payload.send_days):
            raise HTTPException(status_code=400, detail="Kies minstens 1 geldige verzenddag (1=maandag..7=zondag).")
        send_days_str = ",".join(str(d) for d in sorted(set(payload.send_days)))
    settings = database.update_send_schedule_settings(
        account["id"], send_days=send_days_str, send_exclude_holidays_nl=payload.send_exclude_holidays_nl,
    )
    return {"success": True, **settings, "send_days": [int(d) for d in settings["send_days"].split(",") if d]}


@app.get("/api/dashboard/attention")
def _blocked_bad_name_attention_item(account_id: int) -> dict | None:
    """Extra "Aandacht nodig"-item, naast database.attention_items()
    hieronder - de naam-check (_looks_like_real_name) is Python-logica,
    dus dit item wordt hier samengesteld (via de kandidatenlijst uit
    database.contacts_with_pending_automated_sends) i.p.v. in SQL."""
    candidates = database.contacts_with_pending_automated_sends(account_id)
    bad = [c for c in candidates if not _looks_like_real_name(c["first_name"])]
    if not bad:
        return None
    return {
        "type": "blocked_bad_name", "severity": "medium",
        "message": f"{len(bad)} contact(en) hebben geen bruikbare voornaam - automatische mails worden tegengehouden totdat je dit herstelt.",
        "count": len(bad), "tab": "contacts",
    }


@app.get("/api/dashboard/attention")
def api_dashboard_attention(account: dict = Depends(get_current_account)):
    """"Aandacht nodig"-kaart op het Dashboard-tabblad: mislukte
    verzendingen, openstaande conceptantwoorden, vervallen herinneringen,
    een eventuele verzendwachtrij (database.attention_items()) en
    contacten met een kapotte naam die verzending blokkeert."""
    items = database.attention_items(account["id"])
    bad_name_item = _blocked_bad_name_attention_item(account["id"])
    if bad_name_item:
        items.append(bad_name_item)
        severity_order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda i: severity_order.get(i["severity"], 9))
    return {"items": items}


@app.get("/api/dashboard/overview-stats")
def api_dashboard_overview_stats(account: dict = Depends(get_current_account)):
    """Paneel-cijfers voor de Dashboard-tab (Contacten/Campagnes/Sequenties/
    Replies & taken/Systeem) - geïnspireerd op een screenshot van Payt's
    beheer-dashboard, vervangt de eerdere hardcoded placeholders daar.
    ai_configured komt hier i.p.v. in database.py bij, want dat is een
    losstaande module-check (ANTHROPIC_API_KEY), geen databasequery."""
    stats = database.dashboard_overview_stats(account["id"])
    stats["system"]["ai_configured"] = ai_client.is_configured()
    return stats


@app.get("/api/dashboard/flows-attention")
def api_dashboard_flows_attention(account: dict = Depends(get_current_account)):
    """"Flows die aandacht nodig hebben"-blok: sequences/campagnes die de
    flow-monitor automatisch heeft gepauzeerd wegens een te lage
    reply-rate - zie database.attention_flows()."""
    return database.attention_flows(account["id"])


@app.get("/api/dashboard/scheduled-sends")
def api_dashboard_scheduled_sends(account: dict = Depends(get_current_account)):
    """"Geplande mails"-blok op het Dashboard-tabblad: wat er nog klaarstaat
    om verstuurd te worden (crm-roadmap.md, "overzicht van geplande
    mails") - het spiegelbeeld van "Flows die aandacht nodig hebben"
    hierboven: dat toont wat er MIS dreigt te gaan, dit toont wat er nog
    KOMT. Zie database.scheduled_sends_overview()."""
    return database.scheduled_sends_overview(account["id"])


@app.post("/api/campaigns/{campaign_id}/resume")
def api_resume_campaign(campaign_id: int, account: dict = Depends(require_admin_role)):
    if not database.resume_campaign(campaign_id, account["id"]):
        raise HTTPException(status_code=404, detail="Campagne niet gevonden.")
    return {"success": True}


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
            first_name=row.get("first_name", ""),  # leeg blijft leeg - géén e-mail-local-part als naam-fallback (zie _looks_like_real_name)
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
    q: str = None, tag: list[str] = Query(default=[]), persona_id: list[int] = Query(default=[]),
    assigned_to: list[str] = Query(default=[]),
    exclude_excluded: bool = False, exclude_dnc: bool = False, status: list[str] = Query(default=[]),
    source: list[str] = Query(default=[]),
    account: dict = Depends(get_current_account),
):
    """q searches first/last name, e-mail and company. tag/persona_id/
    assigned_to/status/source zijn elk multiselect (herhaal de query-param
    voor meerdere waardes, bv. ?status=customer&status=excluded) - OR
    binnen hetzelfde filter, AND tussen filters onderling; leeg = niet
    filteren op dat veld. assigned_to accepteert user-id's en/of "none"
    voor niet-toegewezen. status: customer/open_quote/do_not_contact/
    excluded. source: manual/csv/vibe_prospecting/vibe_prospecting_daily."""
    aid = account["id"]
    contacts = database.list_contacts(
        aid, q=q, tag=tag, persona_id=persona_id, assigned_to=assigned_to,
        exclude_excluded=exclude_excluded, exclude_dnc=exclude_dnc, status=status, source=source,
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


@app.delete("/api/contacts/{contact_id}")
def api_delete_contact(contact_id: int, account: dict = Depends(get_current_account)):
    """"Verwijderen" vanuit de normale Contacten-lijst is nu een
    soft-delete (prullenbak, zie GET /api/contacts/deleted hieronder) -
    definitief verwijderen kan alleen nog vanuit die prullenbak, met een
    expliciete "DELETE"-bevestiging."""
    if not database.soft_delete_contact(contact_id, account["id"], deleted_by=account["user_id"]):
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"success": True}


@app.get("/api/contacts/deleted")
def api_list_deleted_contacts(account: dict = Depends(get_current_account)):
    return {"contacts": database.list_deleted_contacts(account["id"])}


@app.post("/api/contacts/{contact_id}/restore")
def api_restore_contact(contact_id: int, account: dict = Depends(get_current_account)):
    if not database.restore_contact(contact_id, account["id"]):
        raise HTTPException(status_code=404, detail="Contact niet gevonden in de prullenbak.")
    return {"success": True}


class PermanentDeleteContactIn(BaseModel):
    confirm: str = ""


@app.post("/api/contacts/{contact_id}/permanent-delete")
def api_permanent_delete_contact(contact_id: int, payload: PermanentDeleteContactIn, account: dict = Depends(get_current_account)):
    """Definitief en onomkeerbaar verwijderen - alleen aan te roepen vanuit
    de prullenbak. Vereist dat de gebruiker letterlijk "DELETE" typt als
    extra veiligheidsstap, bovenop de soft-delete die al gebeurd is."""
    if payload.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='Typ precies "DELETE" om definitief te verwijderen.')
    if not database.delete_contact(contact_id, account["id"]):
        raise HTTPException(status_code=404, detail="Contact niet gevonden.")
    return {"success": True}


class BulkContactIdsIn(BaseModel):
    contact_ids: list[int]


@app.post("/api/contacts/bulk-delete")
def api_bulk_delete_contacts(payload: BulkContactIdsIn, account: dict = Depends(get_current_account)):
    """Soft-delete (prullenbak) - zie GET /api/contacts/deleted en
    POST /api/contacts/{id}/restore. Definitief verwijderen kan alleen
    nog vanuit de prullenbak, met een expliciete "DELETE"-bevestiging."""
    deleted = sum(
        1 for cid in payload.contact_ids
        if database.soft_delete_contact(cid, account["id"], deleted_by=account["user_id"])
    )
    return {"success": True, "deleted": deleted, "skipped": len(payload.contact_ids) - deleted}


class BulkContactFlagIn(BaseModel):
    contact_ids: list[int]
    do_not_contact: bool | None = None
    is_customer: bool | None = None
    has_open_quote: bool | None = None


@app.post("/api/contacts/bulk-flag")
def api_bulk_flag_contacts(payload: BulkContactFlagIn, account: dict = Depends(get_current_account)):
    """Bulk-versie van PATCH /api/contacts/{id} voor de status-vlaggen -
    exclude_unset zodat alleen de expliciet meegegeven vlag wordt gezet,
    net als bij de single-contact route."""
    fields = payload.model_dump(exclude={"contact_ids"}, exclude_unset=True)
    updated = sum(1 for cid in payload.contact_ids if database.update_contact(cid, account["id"], **fields))
    return {"success": True, "updated": updated}


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
            first_name=row.get("first_name", ""),  # leeg blijft leeg - géén e-mail-local-part als naam-fallback (zie _looks_like_real_name)
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
    pain_points: list[str] = []


@app.put("/api/account-profile")
def api_update_account_profile(payload: AccountProfileIn, account: dict = Depends(get_current_account)):
    profile = database.upsert_account_profile(account["id"], payload.value_proposition, payload.usps, payload.pain_points)
    return {"success": True, "profile": profile}


@app.post("/api/account-profile/generate-questions")
def api_generate_profile_questions(account: dict = Depends(get_current_account)):
    profile = database.get_account_profile(account["id"])
    personas = database.list_buyer_personas(account["id"])
    questions, source = _FALLBACK_PROFILE_QUESTIONS, "template"
    if ai_client.is_configured():
        try:
            questions = ai_client.generate_profile_questions(
                profile["value_proposition"], profile["usps"], personas, profile["pain_points"]
            )
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
                profile["value_proposition"], profile["usps"], persona, count, pain_points=profile["pain_points"]
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
# Email Generator: statische Horeca-KB + AI-gegenereerde cold-outreach
# e-mails + opgeslagen templates-bibliotheek.
# ---------------------------------------------------------------------------

_HORECA_KB = {
    "pains": [
        "Klanten betalen veel te laat - we moeten weken wachten op betaling terwijl wij zelf al hebben betaald",
        "Cashflow problemen - we kunnen onze eigen vaste kosten niet betalen door openstaande facturen",
        "Steeds vaker totale wanbetalers - klanten die hun schuld nooit betalen",
        "Veel tijd kwijt aan herinneren en mahningen sturen",
        "Te veel risico op klantfaillissementen - we zitten voor grote bedragen vast",
        "Admin werk voor incasso is groot en kost kostbare uren van het team",
        "Klanten kunnen niet betalen omdat zij zelf geen cashflow hebben",
        "Voorraden voorschieten aan klanten die misschien niet betalen - groot risico",
        "Geen inzicht in welke klanten betalingsproblemen krijgen - we merken het te laat",
    ],
    "values": [
        "Automatische herinneringen - bespaar uren handwerk",
        "Betere cashflow - sneller geld in = sneller betalen aan leveranciers",
        "Vroeger zien wie niet kan betalen - voorkomen van grote schrijfverlies",
        "Incasso wordt geautomatiseerd - minder ingewikkeld administratief werk",
        "Betalingsopties voor klanten - maak betalen makkelijker, krijg sneller je geld",
        "Dashboard met klantenstatus - weet exact wie risico is",
        "Minder faillissementen omdat klanten betaalmogelijkheden krijgen",
        "Schaal je bedrijf op zonder meer admin werk",
        "Focus op verkopen, niet op incasso - dat doet het systeem",
    ],
    "objections": [
        "We hebben al weinig marge - dit maakt het nog ingewikkelder",
        "Onze klanten zijn klein, veel cash betaalmiddel",
        "Dit voelt als extra werk erbij, geen besparing",
        "Te technisch - wij zijn geen IT-bedrijf",
        "Hoeveel gaat dit kosten? We zien niet direct voordeel",
        "Onze klanten willen niet in een systeem, zij willen bellen/direct regelen",
        "Dit verandert onze manier van werken - risico",
        "Kleine klanten betalen toch gewoon, grote klanten die betalen laat hebben we al in de gaten",
    ],
}

_FALLBACK_PERSONA_FOCUS = {
    "owner": "jullie cashflow en omzetrisico",
    "manager": "de tijd die het team kwijt is aan opvolgen",
    "admin": "het handmatige werk in het incassoproces",
    "it": "hoe eenvoudig en veilig dit te koppelen is aan jullie systemen",
    "": "wat dit voor {{company}} kan betekenen",
}

# Niet-AI fallback - bewust per fase een eigen set van 3 voorbeeld-mails
# (niet één generieke lijst voor alle fases), zodat de output ook zonder
# ANTHROPIC_API_KEY nog herkenbaar verschilt per gekozen fase. `{focus}`
# wordt per persona ingevuld via _FALLBACK_PERSONA_FOCUS, zie
# _fallback_outreach_emails().
_FALLBACK_STAGE_EMAILS = {
    "1": [
        {
            "subject": "{{firstName}}, herkenbaar bij {{company}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Veel horeca-groothandels waar ik mee spreek, merken dat "
                "betalingen van klanten steeds langer duren en dat het versturen van herinneringen "
                "best wat tijd kost. Vaak blijft dat onderbelicht, terwijl het wel invloed heeft op "
                "{focus}.<br><br>Speelt dit ook bij {{company}}? Ben benieuwd hoe jullie dit nu "
                "aanpakken - geen verplichting, gewoon nieuwsgierig."
            ),
            "angle": "Introductie: herkenbare situatie benoemen, geen hard aanbod",
        },
        {
            "subject": "Kort voorstellen, {{firstName}}",
            "body": (
                "Hoi {{firstName}},<br><br>Ik help horeca-groothandels zoals {{company}} met het "
                "automatiseren van betalingsherinneringen en incasso, zodat er minder tijd in "
                "handmatig opvolgen gaat zitten en {focus} verbetert.<br><br>Nog geen idee of dit "
                "voor jullie relevant is - vandaar dit korte berichtje. Interessant genoeg om even "
                "kennis te maken?"
            ),
            "angle": "Introductie: zachte kennismaking met wat Twikey doet",
        },
        {
            "subject": "Even een korte vraag, {{firstName}}",
            "body": (
                "Hoi {{firstName}},<br><br>Bij veel horeca-groothandelbedrijven loopt het opvolgen "
                "van openstaande facturen op tot flink wat uren per week - vaak zonder dat er echt "
                "naar gekeken wordt.<br><br>Is dat bij {{company}} ook een onderwerp waar wel wat "
                "tijd in gaat zitten? Ben benieuwd naar jullie situatie."
            ),
            "angle": "Introductie: open vraag als opener",
        },
        {
            "subject": "Nieuwsgierig naar jullie aanpak, {{firstName}}",
            "body": (
                "Hoi {{firstName}},<br><br>Ik kom regelmatig horeca-groothandels tegen die worstelen "
                "met het op tijd binnenkrijgen van betalingen, zonder dat er echt een vast proces "
                "voor is.<br><br>Hoe pakken jullie dat bij {{company}} eigenlijk aan? Puur uit "
                "nieuwsgierigheid - ben benieuwd of {focus} daar al een rol in speelt."
            ),
            "angle": "Introductie: nieuwsgierige vraag naar huidige aanpak",
        },
        {
            "subject": "{{firstName}}, mag ik iets vragen?",
            "body": (
                "Hoi {{firstName}},<br><br>Ik werk veel met horeca-groothandels en zie vaak hetzelfde "
                "patroon: betalingen die uitlopen, en niemand die er structureel tijd voor vrijmaakt "
                "om dat bij te sturen.<br><br>Is dat bij {{company}} ook zo, of hebben jullie dit al "
                "goed voor elkaar? Gewoon benieuwd."
            ),
            "angle": "Introductie: patroon herkennen, open uitnodiging tot reactie",
        },
    ],
    "2": [
        {
            "subject": "Hoeveel dagen wachten jullie op betaling?",
            "body": (
                "Hoi {{firstName}},<br><br>Een concrete vraag: gemiddeld hoeveel dagen wachten jullie "
                "bij {{company}} op betaling van klanten, en hoeveel tijd gaat er wekelijks in het "
                "versturen van herinneringen?<br><br>Bij veel horeca-groothandels loopt dit flink op "
                "en raakt het direct {focus}. Benieuwd of dat bij jullie ook speelt."
            ),
            "angle": "Probleem herkenning: concreet doorvragen op het pijnpunt",
        },
        {
            "subject": "Late betalingen - herkenbaar?",
            "body": (
                "Hoi {{firstName}},<br><br>Steeds meer horeca-groothandels geven aan dat late "
                "betalingen en het bijhouden van wie nog moet betalen veel tijd en energie kost - en "
                "dat dit uiteindelijk {focus} onder druk zet.<br><br>Is dit ook een terugkerend "
                "gesprek binnen {{company}}? Zou graag horen hoe jullie dit nu oplossen."
            ),
            "angle": "Probleem herkenning: bredere trend + persoonlijke vraag",
        },
        {
            "subject": "Wie houdt bij {{company}} de betalingen bij?",
            "body": (
                "Hoi {{firstName}},<br><br>Bij veel bedrijven in de horeca-groothandel is er niemand "
                "die structureel bijhoudt welke klanten traag betalen, totdat het een probleem wordt. "
                "Dat kost vaak meer dan gedacht, ook qua {focus}.<br><br>Hoe gaat dit nu bij "
                "{{company}}? Benieuwd of dit al ergens op de agenda staat."
            ),
            "angle": "Probleem herkenning: rol/verantwoordelijkheid als invalshoek",
        },
        {
            "subject": "Wat kost het opvolgen van facturen jullie echt?",
            "body": (
                "Hoi {{firstName}},<br><br>Vaak wordt onderschat hoeveel tijd het kost om achter "
                "openstaande facturen aan te zitten - bellen, mailen, nog een keer bellen. Bij "
                "{{company}} speelt dat vast ook, en het raakt direct {focus}.<br><br>Heb je hier al "
                "eens concreet naar gekeken, of is het meer iets wat er gewoon bij hoort?"
            ),
            "angle": "Probleem herkenning: kosten van handmatig opvolgen concreet maken",
        },
        {
            "subject": "Herkenbaar: laatste-moment betalers?",
            "body": (
                "Hoi {{firstName}},<br><br>Bij veel horeca-groothandels is er een vaste groep klanten "
                "die altijd tot het laatste moment wacht met betalen - en dat kost tijd, energie en "
                "uiteindelijk {focus}.<br><br>Speelt dat bij {{company}} ook, en hoe gaan jullie daar "
                "nu mee om?"
            ),
            "angle": "Probleem herkenning: specifiek klantgedrag als herkenningspunt",
        },
    ],
    "3": [
        {
            "subject": "Zo lossen horeca-groothandels dit op",
            "body": (
                "Hoi {{firstName}},<br><br>Twikey automatiseert betalingsherinneringen en incasso "
                "specifiek voor horeca-groothandels, waardoor {focus} merkbaar verbetert - zonder dat "
                "jullie team er extra werk aan heeft.<br><br>Klanten van ons zien doorgaans binnen "
                "enkele weken al minder openstaande facturen. Wil je kort zien hoe dat er voor "
                "{{company}} uit zou zien?"
            ),
            "angle": "Oplossing interesse: concreet resultaat + zachte CTA",
        },
        {
            "subject": "Minder handwerk, sneller betaald",
            "body": (
                "Hoi {{firstName}},<br><br>In plaats van zelf herinneringen te versturen en bij te "
                "houden wie nog moet betalen, regelt Twikey dit automatisch - wat direct {focus} ten "
                "goede komt.<br><br>Voor {{company}} zou dit betekenen dat het team minder tijd kwijt "
                "is aan opvolgen. Interessant om hier kort kennis mee te maken?"
            ),
            "angle": "Oplossing interesse: automatisering als kernvoordeel",
        },
        {
            "subject": "Een concreet voorbeeld voor {{company}}",
            "body": (
                "Hoi {{firstName}},<br><br>Andere horeca-groothandels die met Twikey werken, zien dat "
                "betalingsherinneringen vanzelf gaan en dat {focus} daardoor verbetert, zonder dat er "
                "iemand achteraan hoeft te bellen.<br><br>Zou je het interessant vinden om te zien hoe "
                "dat er specifiek voor {{company}} uit zou kunnen zien?"
            ),
            "angle": "Oplossing interesse: social proof + uitnodiging",
        },
        {
            "subject": "Van achteraf bellen naar automatisch herinneren",
            "body": (
                "Hoi {{firstName}},<br><br>In plaats van zelf achter betalingen aan te bellen, zorgt "
                "Twikey ervoor dat herinneringen automatisch en op het juiste moment verstuurd worden "
                "- wat direct {focus} verbetert.<br><br>Klinkt dit als iets wat waarde zou kunnen "
                "hebben voor {{company}}?"
            ),
            "angle": "Oplossing interesse: van handmatig naar automatisch als kernboodschap",
        },
        {
            "subject": "Eén systeem, minder gedoe",
            "body": (
                "Hoi {{firstName}},<br><br>Twikey geeft horeca-groothandels één overzichtelijk "
                "systeem voor betalingsherinneringen en incasso, in plaats van losse mailtjes en "
                "telefoontjes - met merkbaar effect op {focus}.<br><br>Zou je het interessant vinden "
                "om te zien hoe dat er voor {{company}} uit zou zien?"
            ),
            "angle": "Oplossing interesse: overzicht/eenvoud als invalshoek",
        },
    ],
    "4": [
        {
            "subject": "15 minuten deze week, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Zullen we deze week 15 minuten inplannen om te bespreken "
                "hoe Twikey {focus} kan verbeteren bij {{company}}? Ik laat je graag concreet zien wat "
                "dit in de praktijk oplevert, zonder verplichtingen.<br><br>Welke dag komt jou het "
                "beste uit?"
            ),
            "angle": "Engagement: directe afspraak-CTA",
        },
        {
            "subject": "Zullen we bellen, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Ik denk dat een kort gesprek van 10 à 15 minuten al "
                "duidelijk kan maken of Twikey iets voor {{company}} kan betekenen, vooral op het "
                "gebied van {focus}.<br><br>Kan ik je deze of volgende week even bellen? Zeg maar "
                "welk moment schikt."
            ),
            "angle": "Engagement: telefonisch contact als lagedrempel-CTA",
        },
        {
            "subject": "Klaar voor de volgende stap, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>We hebben het eerder al even gehad over "
                "betalingsherinneringen bij {{company}} - ik denk dat het nu een goed moment is om "
                "samen te kijken wat dit concreet kan opleveren voor {focus}.<br><br>Zullen we een "
                "korte call inplannen deze week?"
            ),
            "angle": "Engagement: vervolgstap na eerder contact",
        },
        {
            "subject": "Kort telefoontje, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Ik wil je niet te veel van je tijd vragen - een "
                "telefoontje van 10 minuten is genoeg om te bepalen of dit interessant is voor "
                "{{company}}, met name op het gebied van {focus}.<br><br>Zullen we deze week iets "
                "inplannen?"
            ),
            "angle": "Engagement: laagdrempelig telefonisch contact",
        },
        {
            "subject": "Wanneer komt het jou uit, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Ik denk dat we in een kort gesprek al snel kunnen bepalen "
                "of dit voor {{company}} de moeite waard is, vooral gezien {focus}.<br><br>Laat "
                "gerust weten welk moment deze of volgende week jou uitkomt, dan plan ik het in."
            ),
            "angle": "Engagement: flexibele planning als CTA",
        },
    ],
    "5": [
        {
            "subject": "Laatste check, {{firstName}}",
            "body": (
                "Hoi {{firstName}},<br><br>Ik wil je niet langer lastigvallen - dit is mijn laatste "
                "bericht hierover.<br><br>Als {focus} relevant is voor {{company}}, laat het gerust "
                "weten. Anders hoor je niets meer van me."
            ),
            "angle": "Urgentie: korte, laagdrempelige laatste poging",
        },
        {
            "subject": "Nog interesse, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Korte laatste vraag: is dit nog relevant voor {{company}}? "
                "Zo niet, geen probleem - dan laat ik het hierbij.<br><br>Een simpel 'ja' of 'nee' is "
                "al genoeg."
            ),
            "angle": "Urgentie: minimale inspanning gevraagd",
        },
        {
            "subject": "Sluit ik dit af, {{firstName}}?",
            "body": (
                "Hoi {{firstName}},<br><br>Ik neem aan dat de timing nu niet goed is en sluit dit "
                "dossier voor {{company}} dan ook af, tenzij ik voor vrijdag nog iets van je "
                "hoor.<br><br>Mocht {focus} later alsnog relevant worden, hoor ik het graag."
            ),
            "angle": "Urgentie: aanname + deadline als duw",
        },
        {
            "subject": "Definitief laatste bericht, {{firstName}}",
            "body": (
                "Hoi {{firstName}},<br><br>Dit is echt mijn laatste poging - ik wil je tijd niet "
                "verder in beslag nemen.<br><br>Mocht {focus} op enig moment weer relevant worden "
                "voor {{company}}, hoor ik het graag. Tot dan wens ik je succes."
            ),
            "angle": "Urgentie: vriendelijke afsluiting met open deur",
        },
        {
            "subject": "Nog één keer, {{firstName}}",
            "body": (
                "Hoi {{firstName}},<br><br>Ik snap het als de timing nu niet goed is - dit is dan ook "
                "echt de laatste keer dat ik hierover mail.<br><br>Een kort 'nee, bedankt' is ook "
                "prima, dan weet ik dat en stop ik ermee."
            ),
            "angle": "Urgentie: expliciete uitnodiging om af te wijzen",
        },
    ],
}


def _fallback_outreach_emails(persona: str, stage: str, count: int) -> list:
    """Niet-AI fallback (zie api_generate_outreach_emails) - per fase een
    eigen set voorbeelden i.p.v. één generieke lijst, met een
    persona-specifieke focus verweven in de tekst via {focus}."""
    focus = _FALLBACK_PERSONA_FOCUS.get(persona, _FALLBACK_PERSONA_FOCUS[""])
    variants = _FALLBACK_STAGE_EMAILS.get(stage, _FALLBACK_STAGE_EMAILS["1"])
    # .replace(), not .format(): these bodies contain literal {{firstName}}/
    # {{company}} merge fields that must survive untouched - str.format()
    # would collapse the doubled braces to {firstName}/{company}.
    emails = [
        {"subject": v["subject"], "body": v["body"].replace("{focus}", focus), "angle": v["angle"]}
        for v in variants
    ]
    return emails[:count]


class EmailGeneratorKbIn(BaseModel):
    sector: str = "horeca"


@app.post("/api/email-generator/kb")
def api_email_generator_kb(payload: EmailGeneratorKbIn, account: dict = Depends(get_current_account)):
    """Geeft de statische Horeca-knowledge-base terug - geen AI-aanroep,
    vaste referentiedata, zie _HORECA_KB."""
    if payload.sector != "horeca":
        raise HTTPException(status_code=400, detail="Deze sector wordt nog niet ondersteund.")
    return {"success": True, "sector": payload.sector, "kb": _HORECA_KB}


class EmailGeneratorEmailsIn(BaseModel):
    sector: str = "horeca"
    persona: str
    goal: str
    stage: str
    tone: str
    pains: list[str] = []
    values: list[str] = []
    objections: list[str] = []
    count: int = 3


@app.post("/api/email-generator/emails")
def api_generate_outreach_emails(payload: EmailGeneratorEmailsIn, account: dict = Depends(get_current_account)):
    count = max(1, min(payload.count, 5))
    source = "template"
    emails = None
    if ai_client.is_configured():
        try:
            emails = ai_client.generate_outreach_emails(
                payload.persona, payload.goal, payload.stage, payload.tone,
                payload.pains, payload.values, payload.objections,
                sector=payload.sector, count=count,
            )
            source = "ai"
        except Exception as exc:  # noqa: BLE001 - fall back to the static examples below
            logger.warning("AI-outreach-mails genereren mislukt voor account %s: %s", account["id"], exc)
    if not emails:
        emails = _fallback_outreach_emails(payload.persona, payload.stage, count)
    return {"success": True, "source": source, "emails": emails}


class EmailGeneratorTemplateIn(BaseModel):
    sector: str = "horeca"
    persona: str = ""
    goal: str = ""
    stage: str = ""
    tone: str = ""
    subject: str
    body: str
    angle: str = ""


@app.post("/api/email-generator/templates")
def api_save_email_generator_template(payload: EmailGeneratorTemplateIn, account: dict = Depends(get_current_account)):
    template = database.create_email_generator_template(
        account["id"], payload.sector, payload.persona, payload.goal, payload.stage,
        payload.tone, payload.subject, payload.body, payload.angle,
    )
    return {"success": True, "template": template}


@app.get("/api/email-generator/templates")
def api_list_email_generator_templates(account: dict = Depends(get_current_account)):
    return {"templates": database.list_email_generator_templates(account["id"])}


class EmailGeneratorTemplateEditIn(BaseModel):
    subject: str | None = None
    body: str | None = None
    angle: str | None = None


@app.put("/api/email-generator/templates/{template_id}")
def api_update_email_generator_template(template_id: int, payload: EmailGeneratorTemplateEditIn,
                                         account: dict = Depends(get_current_account)):
    row = database.update_email_generator_template(
        template_id, account["id"], payload.subject, payload.body, payload.angle
    )
    if not row:
        raise HTTPException(status_code=404, detail="Template niet gevonden.")
    return {"success": True, "template": row}


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
    contact_id: int | None = None
    contact_name: str = ""  # nodig als contact_id leeg is - een herinnering moet ergens over gaan
    remind_at: str  # ISO date/datetime
    note: str = ""


@app.post("/api/reminders")
def api_create_reminder(payload: ReminderIn, account: dict = Depends(get_current_account)):
    if not payload.contact_id and not payload.contact_name.strip():
        raise HTTPException(status_code=400, detail="Kies een contact of vul een naam in.")
    reminder = database.create_reminder(
        account["id"], payload.contact_id, payload.remind_at, payload.note,
        created_by=account.get("user_id"), contact_name=payload.contact_name,
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
    api_key: str | None = None  # leeg = huidige key behouden (alleen de andere velden bijwerken)
    daily_import_enabled: bool = False
    daily_import_count: int = 10
    daily_import_sector: str = ''
    daily_import_country: str = 'nl'
    daily_import_auto_enroll_sequence_id: int | None = None  # None = uit, blijft de bewuste handmatige stap


@app.get("/api/integrations/prospecting")
def api_get_prospecting_settings(account: dict = Depends(get_current_account)):
    row = database.get_prospecting_settings(account["id"])
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "daily_import_enabled": bool(row["daily_import_enabled"]),
        "daily_import_count": row["daily_import_count"],
        "daily_import_sector": row["daily_import_sector"],
        "daily_import_country": row["daily_import_country"],
        "daily_import_auto_enroll_sequence_id": row["daily_import_auto_enroll_sequence_id"],
    }


@app.post("/api/integrations/prospecting")
def api_save_prospecting_settings(payload: ProspectingSettingsIn, account: dict = Depends(get_current_account)):
    api_key_encrypted = crypto.encrypt(payload.api_key.strip()) if payload.api_key and payload.api_key.strip() else None
    try:
        database.save_prospecting_settings(
            account["id"], api_key_encrypted, payload.daily_import_enabled,
            payload.daily_import_count, payload.daily_import_sector.strip(),
            payload.daily_import_country.strip().lower() or 'nl',
            payload.daily_import_auto_enroll_sequence_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


class LinkedinAdsSettingsIn(BaseModel):
    access_token: str = ""  # leeg = huidige token behouden
    sponsored_account_urn: str
    enabled: bool = False
    auto_enroll_sequence_id: int | None = None


@app.get("/api/integrations/linkedin-ads")
def api_get_linkedin_ads_settings(account: dict = Depends(get_current_account)):
    row = database.get_linkedin_ads_settings(account["id"])
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "sponsored_account_urn": row["sponsored_account_urn"],
        "enabled": bool(row["enabled"]),
        "auto_enroll_sequence_id": row["auto_enroll_sequence_id"],
        "last_synced_at": row["last_synced_at"],
    }


@app.post("/api/integrations/linkedin-ads")
def api_save_linkedin_ads_settings(payload: LinkedinAdsSettingsIn, account: dict = Depends(get_current_account)):
    existing = database.get_linkedin_ads_settings(account["id"])
    if payload.access_token.strip():
        access_token_encrypted = crypto.encrypt(payload.access_token.strip())
    elif existing:
        access_token_encrypted = existing["access_token_encrypted"]
    else:
        raise HTTPException(status_code=400, detail="Access token is verplicht bij het voor het eerst instellen.")
    if not payload.sponsored_account_urn.strip():
        raise HTTPException(status_code=400, detail="Sponsored Account URN mag niet leeg zijn.")
    database.save_linkedin_ads_settings(
        account["id"], access_token_encrypted, payload.sponsored_account_urn.strip(),
        payload.enabled, payload.auto_enroll_sequence_id,
    )
    return {"success": True}


@app.delete("/api/integrations/linkedin-ads")
def api_delete_linkedin_ads_settings(account: dict = Depends(get_current_account)):
    database.delete_linkedin_ads_settings(account["id"])
    return {"success": True}


class MetaAdsSettingsIn(BaseModel):
    access_token: str = ""  # leeg = huidige token behouden
    form_ids: str  # comma-gescheiden Lead Gen Form-ID's
    enabled: bool = False
    auto_enroll_sequence_id: int | None = None


@app.get("/api/integrations/meta-ads")
def api_get_meta_ads_settings(account: dict = Depends(get_current_account)):
    row = database.get_meta_ads_settings(account["id"])
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "form_ids": row["form_ids"],
        "enabled": bool(row["enabled"]),
        "auto_enroll_sequence_id": row["auto_enroll_sequence_id"],
        "last_synced_at": row["last_synced_at"],
    }


@app.post("/api/integrations/meta-ads")
def api_save_meta_ads_settings(payload: MetaAdsSettingsIn, account: dict = Depends(get_current_account)):
    existing = database.get_meta_ads_settings(account["id"])
    if payload.access_token.strip():
        access_token_encrypted = crypto.encrypt(payload.access_token.strip())
    elif existing:
        access_token_encrypted = existing["access_token_encrypted"]
    else:
        raise HTTPException(status_code=400, detail="Access token is verplicht bij het voor het eerst instellen.")
    form_ids = ",".join(f.strip() for f in payload.form_ids.split(",") if f.strip())
    if not form_ids:
        raise HTTPException(status_code=400, detail="Vul minstens 1 Lead Gen Form-ID in.")
    database.save_meta_ads_settings(
        account["id"], access_token_encrypted, form_ids, payload.enabled, payload.auto_enroll_sequence_id,
    )
    return {"success": True}


@app.delete("/api/integrations/meta-ads")
def api_delete_meta_ads_settings(account: dict = Depends(get_current_account)):
    database.delete_meta_ads_settings(account["id"])
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
    country: str | None = None  # 2-letter code, bv. 'nl' - filtert lookalikes op hetzelfde land


@app.post("/api/prospecting/businesses/lookalikes")
def api_prospecting_lookalikes(payload: ProspectingLookalikeIn, account: dict = Depends(get_current_account)):
    """Lookalikes (crm-roadmap.md punt 3): bedrijven die lijken op een al
    gematchte business_id."""
    api_key = _decrypted_prospecting_key(account["id"])
    try:
        results = prospecting_client.search_lookalike_businesses(
            api_key, payload.business_id, payload.size, country=payload.country
        )
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
    """Ondanks de route-naam ("match") roept dit fetch_prospects() aan als
    er een business_id is meegegeven - een kale business_id is bij
    Explorium geen geldige match-invoer (match_prospects verwacht een al
    bekend, specifiek persoon), dus "vind mensen bij dit bedrijf" moet via
    het filter-based search-endpoint. Route-pad en frontend-contract
    (POST met `prospects: [{business_id, job_titles}]`, terug: `prospects:
    [{prospect_id, full_name, ...}]`) blijven ongewijzigd."""
    api_key = _decrypted_prospecting_key(account["id"])
    try:
        results = []
        for p in payload.prospects:
            if p.business_id:
                filters = {"business_id": {"values": [p.business_id]}}
                if p.job_titles:
                    filters["job_title"] = {"values": p.job_titles, "include_related_job_titles": True}
                results.extend(prospecting_client.fetch_prospects(api_key, filters))
            else:
                results.extend(prospecting_client.match_prospects(api_key, [p.model_dump(exclude_none=True)]))
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


# v1 simplificatie: vaste functietitel-lijst i.p.v. nog een instelling -
# makkelijk later te promoveren tot een settings-veld, zelfde patroon als
# daily_import_count, mocht dat nodig blijken.
DAILY_PROSPECTING_JOB_TITLES = ["Eigenaar", "Directeur", "Inkoop"]


@app.post("/api/cron/process-prospecting", dependencies=[Depends(require_admin_secret)])
def api_process_prospecting():
    """Dagelijkse automatische prospecting via Vibe Prospecting/Explorium,
    gefilterd op sector/branche (niet op een lookalike-seed). Importeert
    alleen NIEUWE contacten - nooit automatisch benaderd, dat blijft een
    expliciete actie via de contacten-bulk-select + 'toevoegen aan
    campagne'. Zelfde beveiliging/aanroeppatroon als
    /api/cron/process-sequences, zie DEPLOY.md."""
    accounts_processed, contacts_imported, errors = 0, 0, 0
    sequence_enrolled, sequence_skipped_cooldown = 0, 0
    for settings_row in database.accounts_with_daily_prospecting_enabled():
        account_id = settings_row["account_id"]
        run_id = database.start_import_run(account_id, "vibe_prospecting_daily")
        try:
            new_count, enrolled, skipped_cooldown = _run_prospecting_import_for_account(account_id, settings_row)
            contacts_imported += new_count
            accounts_processed += 1
            sequence_enrolled += enrolled
            sequence_skipped_cooldown += skipped_cooldown
            database.update_prospecting_import_status(account_id, new_count=new_count, error=None)
            database.finish_import_run(run_id, contacts_added=new_count)
        except Exception as exc:  # noqa: BLE001 - één account-fout mag de hele cron-run niet stoppen
            logger.warning("Dagelijkse prospecting mislukt voor account %s: %s", account_id, exc)
            errors += 1
            database.update_prospecting_import_status(account_id, new_count=0, error=str(exc))
            database.finish_import_run(run_id, contacts_added=0, error=str(exc))
    return {
        "success": True, "accounts_processed": accounts_processed, "contacts_imported": contacts_imported,
        "errors": errors, "sequence_enrolled": sequence_enrolled, "sequence_skipped_cooldown": sequence_skipped_cooldown,
    }


def _run_prospecting_import_for_account(account_id: int, settings_row: dict) -> tuple:
    """Kernlogica van de dagelijkse Vibe Prospecting-import voor één
    account - losgetrokken uit api_process_prospecting() hierboven zodat
    zowel de cron als de handmatige 'Nu importeren'-trigger
    (api_prospecting_import_now hieronder) 'm kunnen hergebruiken.
    Retourneert (new_count, sequence_enrolled, sequence_skipped_cooldown);
    gooit door bij een fout - de caller is verantwoordelijk voor het
    afronden van de import_runs-rij (finish_import_run)."""
    target = settings_row["daily_import_count"] or 10
    sector = (settings_row["daily_import_sector"] or "").strip()
    country = (settings_row["daily_import_country"] or "nl").strip().lower()
    auto_enroll_sequence_id = settings_row["daily_import_auto_enroll_sequence_id"]
    api_key = crypto.decrypt(settings_row["api_key_encrypted"])
    filters = {"country_code": {"values": [country]}}
    if sector:
        filters["linkedin_category"] = {"values": [sector]}
    businesses = prospecting_client.search_businesses(api_key, filters, size=target * 3)
    new_count, sequence_enrolled, sequence_skipped_cooldown = 0, 0, 0
    for business in businesses:
        if new_count >= target:
            break
        business_id = business.get("business_id")
        if not business_id:
            continue
        # fetch_prospects (niet match_prospects) is het juiste endpoint om
        # mensen bij een bedrijf te VINDEN - geeft naam/functie meteen mee,
        # dus geen aparte match-stap nodig voor die velden.
        prospects = prospecting_client.fetch_prospects(api_key, {
            "business_id": {"values": [business_id]},
            "job_title": {"values": DAILY_PROSPECTING_JOB_TITLES, "include_related_job_titles": True},
        }, size=target - new_count)
        if not prospects:
            continue
        prospect_ids = [p["prospect_id"] for p in prospects if p.get("prospect_id")]
        enriched_by_id = {e["prospect_id"]: e for e in prospecting_client.enrich_prospect_contacts(api_key, prospect_ids)}
        for prospect in prospects:
            if new_count >= target:
                break
            enriched = enriched_by_id.get(prospect.get("prospect_id"), {})
            email = (enriched.get("email") or "").strip()
            if not email:
                continue
            if database.get_contact_by_email(account_id, email):
                continue  # al bekend - niet als nieuw tellen (add_contact zou 'm alsnog upserten, maar niet dubbel meetellen)
            contact = database.add_contact(
                account_id=account_id,
                first_name=prospect.get("first_name") or (prospect.get("full_name") or "Onbekend").split(" ")[0],
                last_name=prospect.get("last_name") or "",
                job_title=prospect.get("job_title") or "",
                email=email, company=business.get("name") or "", sector=sector,
                source="vibe_prospecting_daily",
            )
            _apply_hubspot_exclusion(account_id, contact)
            new_count += 1
            if auto_enroll_sequence_id:
                result = database.enroll_contact(auto_enroll_sequence_id, account_id, contact["id"])
                if result["enrollment"]:
                    sequence_enrolled += 1
                elif result["skipped_reason"] == "cooldown":
                    sequence_skipped_cooldown += 1
    return new_count, sequence_enrolled, sequence_skipped_cooldown


@app.post("/api/prospecting/import-now")
def api_prospecting_import_now(account: dict = Depends(get_current_account)):
    """Handmatige trigger (Imports-tabblad, 'Nu importeren') - voert
    dezelfde import direct uit i.p.v. te wachten op de dagelijkse cron.
    Werkt ongeacht daily_import_enabled (dat schakelt alleen de
    automatische cron), zolang er een API-key gekoppeld is."""
    settings_row = database.get_prospecting_settings(account["id"])
    if not settings_row or not settings_row.get("api_key_encrypted"):
        raise HTTPException(
            status_code=400,
            detail="Er is nog geen Vibe Prospecting API-key gekoppeld (zie Integraties).",
        )
    run_id = database.start_import_run(account["id"], "vibe_prospecting_manual")
    try:
        new_count, enrolled, skipped_cooldown = _run_prospecting_import_for_account(account["id"], settings_row)
        database.update_prospecting_import_status(account["id"], new_count=new_count, error=None)
        database.finish_import_run(run_id, contacts_added=new_count)
        return {
            "success": True, "contacts_imported": new_count,
            "sequence_enrolled": enrolled, "sequence_skipped_cooldown": skipped_cooldown,
        }
    except Exception as exc:  # noqa: BLE001 - foutmelding teruggeven aan de gebruiker, niet laten crashen
        database.update_prospecting_import_status(account["id"], new_count=0, error=str(exc))
        database.finish_import_run(run_id, contacts_added=0, error=str(exc))
        raise HTTPException(status_code=400, detail=f"Import mislukt: {exc}") from exc


@app.get("/api/imports")
def api_list_imports(account: dict = Depends(get_current_account)):
    """Imports-tabblad: geschiedenis van elke import-poging over alle
    bronnen heen (Vibe Prospecting/LinkedIn Ads/Meta Ads/toekomstige
    bronnen), nieuwste eerst - zie database.list_import_runs()."""
    runs = database.list_import_runs(account["id"])
    for run in runs:
        run["source_label"] = database.SOURCE_LABELS.get(run["source"], run["source"])
    return {"runs": runs}


@app.get("/api/imports/{run_id}")
def api_import_detail(run_id: int, account: dict = Depends(get_current_account)):
    """Detail bij één import-run: welke contacten daarin precies zijn
    toegevoegd (of de foutmelding als de run mislukte) - zie
    database.import_run_detail()."""
    run = database.import_run_detail(run_id, account["id"])
    if not run:
        raise HTTPException(status_code=404, detail="Import-run niet gevonden.")
    run["source_label"] = database.SOURCE_LABELS.get(run["source"], run["source"])
    return run


@app.post("/api/cron/process-linkedin-ads", dependencies=[Depends(require_admin_secret)])
def api_process_linkedin_ads():
    """Periodieke import van nieuwe LinkedIn Lead Gen Form-inzendingen
    (crm-roadmap.md, "leads uit Instagram/LinkedIn-advertenties") - zelfde
    beveiliging/aanroeppatroon en per-account foutisolatie als
    /api/cron/process-prospecting hierboven. Nog niet getest tegen een
    echt account (zie linkedin_ads_client.py) - dit is een eerste,
    syntactisch correcte opzet die pas echt gevalideerd kan worden zodra
    er LinkedIn Lead Sync API-toegang is."""
    accounts_processed, contacts_imported, errors = 0, 0, 0
    sequence_enrolled, sequence_skipped_cooldown = 0, 0
    for settings_row in database.accounts_with_linkedin_ads_enabled():
        account_id = settings_row["account_id"]
        auto_enroll_sequence_id = settings_row["auto_enroll_sequence_id"]
        run_id = database.start_import_run(account_id, "linkedin_ads")
        account_new_count = 0
        try:
            access_token = crypto.decrypt(settings_row["access_token_encrypted"])
            since_ms = None
            if settings_row["last_synced_at"]:
                since_ms = int(datetime.fromisoformat(settings_row["last_synced_at"]).timestamp() * 1000)
            leads = linkedin_ads_client.fetch_new_leads(access_token, settings_row["sponsored_account_urn"], since_ms)
            for lead in leads:
                email = (lead.get("email") or "").strip()
                if not email:
                    continue
                if database.get_contact_by_email(account_id, email):
                    continue  # al bekend - niet als nieuw tellen (add_contact zou 'm alsnog upserten, maar niet dubbel meetellen)
                contact = database.add_contact(
                    account_id=account_id,
                    first_name=lead.get("first_name") or "Onbekend",
                    last_name=lead.get("last_name") or "",
                    job_title=lead.get("job_title") or "",
                    email=email, company=lead.get("company") or "",
                    source="linkedin_ads",
                )
                _apply_hubspot_exclusion(account_id, contact)
                contacts_imported += 1
                account_new_count += 1
                if auto_enroll_sequence_id:
                    result = database.enroll_contact(auto_enroll_sequence_id, account_id, contact["id"])
                    if result["enrollment"]:
                        sequence_enrolled += 1
                    elif result["skipped_reason"] == "cooldown":
                        sequence_skipped_cooldown += 1
            database.update_linkedin_ads_last_synced_at(account_id)
            accounts_processed += 1
            database.finish_import_run(run_id, contacts_added=account_new_count)
        except Exception as exc:  # noqa: BLE001 - één account-fout mag de hele cron-run niet stoppen
            logger.warning("LinkedIn-ads-import mislukt voor account %s: %s", account_id, exc)
            errors += 1
            database.finish_import_run(run_id, contacts_added=account_new_count, error=str(exc))
    return {
        "success": True, "accounts_processed": accounts_processed, "contacts_imported": contacts_imported,
        "errors": errors, "sequence_enrolled": sequence_enrolled, "sequence_skipped_cooldown": sequence_skipped_cooldown,
    }


@app.post("/api/cron/process-meta-ads", dependencies=[Depends(require_admin_secret)])
def api_process_meta_ads():
    """Zelfde als /api/cron/process-linkedin-ads hierboven, maar voor Meta
    (Facebook/Instagram) Lead Ads - zie meta_ads_client.py. Meta bewaart
    leaddata maar 90 dagen, dus deze cron moet minstens zo vaak draaien om
    nooit een lead te missen. Ook hier: nog niet getest tegen een echt
    account, pas mogelijk zodra er leads_retrieval-toegang via Meta App
    Review is."""
    accounts_processed, contacts_imported, errors = 0, 0, 0
    sequence_enrolled, sequence_skipped_cooldown = 0, 0
    for settings_row in database.accounts_with_meta_ads_enabled():
        account_id = settings_row["account_id"]
        auto_enroll_sequence_id = settings_row["auto_enroll_sequence_id"]
        run_id = database.start_import_run(account_id, "meta_ads")
        account_new_count = 0
        try:
            access_token = crypto.decrypt(settings_row["access_token_encrypted"])
            form_ids = [f for f in (settings_row["form_ids"] or "").split(",") if f]
            leads = meta_ads_client.fetch_new_leads(access_token, form_ids, settings_row["last_synced_at"])
            for lead in leads:
                email = (lead.get("email") or "").strip()
                if not email:
                    continue
                if database.get_contact_by_email(account_id, email):
                    continue
                contact = database.add_contact(
                    account_id=account_id,
                    first_name=lead.get("first_name") or "Onbekend",
                    last_name=lead.get("last_name") or "",
                    job_title=lead.get("job_title") or "",
                    email=email, company=lead.get("company") or "",
                    source="meta_ads",
                )
                _apply_hubspot_exclusion(account_id, contact)
                contacts_imported += 1
                account_new_count += 1
                if auto_enroll_sequence_id:
                    result = database.enroll_contact(auto_enroll_sequence_id, account_id, contact["id"])
                    if result["enrollment"]:
                        sequence_enrolled += 1
                    elif result["skipped_reason"] == "cooldown":
                        sequence_skipped_cooldown += 1
            database.update_meta_ads_last_synced_at(account_id)
            accounts_processed += 1
            database.finish_import_run(run_id, contacts_added=account_new_count)
        except Exception as exc:  # noqa: BLE001 - één account-fout mag de hele cron-run niet stoppen
            logger.warning("Meta-ads-import mislukt voor account %s: %s", account_id, exc)
            database.finish_import_run(run_id, contacts_added=account_new_count, error=str(exc))
            errors += 1
    return {
        "success": True, "accounts_processed": accounts_processed, "contacts_imported": contacts_imported,
        "errors": errors, "sequence_enrolled": sequence_enrolled, "sequence_skipped_cooldown": sequence_skipped_cooldown,
    }


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
    _send_plain_for_account(
        account["id"], reply["from_email"], reply_subject, draft_body, sender_user_id=account["user_id"],
    )


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
        if msg.get("is_bounce"):
            # Een DSN/bounce-bericht is geen echte reply - eerder werd dit
            # ten onrechte gecategoriseerd en kreeg het zelfs een AI-
            # conceptantwoord. Markeer i.p.v. daarvan het gebounced adres.
            if msg.get("bounced_recipient"):
                database.mark_contact_bounced(account["id"], msg["bounced_recipient"])
            continue
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
def api_create_sequence(payload: SequenceIn, account: dict = Depends(require_admin_role)):
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
def api_set_sequence_status(sequence_id: int, payload: SequenceStatusIn, account: dict = Depends(require_admin_role)):
    if payload.status not in ("active", "paused"):
        raise HTTPException(status_code=400, detail="Status moet 'active' of 'paused' zijn.")
    if not database.set_sequence_status(sequence_id, account["id"], payload.status):
        raise HTTPException(status_code=404, detail="Sequence niet gevonden.")
    return {"success": True}


class EnrollIn(BaseModel):
    contact_ids: list[int]


@app.post("/api/sequences/{sequence_id}/enroll")
def api_enroll_sequence(sequence_id: int, payload: EnrollIn, account: dict = Depends(get_current_account)):
    enrolled, skipped_cooldown = 0, 0
    for contact_id in payload.contact_ids:
        result = database.enroll_contact(sequence_id, account["id"], contact_id)
        if result["enrollment"]:
            enrolled += 1
        elif result["skipped_reason"] == "cooldown":
            skipped_cooldown += 1
    return {"success": True, "enrolled": enrolled, "skipped_cooldown": skipped_cooldown}


@app.get("/api/sequences/{sequence_id}/enrollments")
def api_list_enrollments(sequence_id: int, account: dict = Depends(get_current_account)):
    """Elke inschrijving krijgt er een rendered_subject/rendered_body bij -
    een preview van de eerstvolgende mail die dit contact zal ontvangen,
    met de merge-velden al ingevuld (zelfde _render_template als bij
    daadwerkelijk versturen, dus wat je hier ziet is exact wat er uit zal
    gaan)."""
    enrollments = database.list_enrollments(sequence_id, account["id"])
    sequence = database.get_sequence(sequence_id, account["id"])
    steps_by_order = {s["step_order"]: s for s in sequence["steps"]} if sequence else {}
    for e in enrollments:
        step = steps_by_order.get(e["current_step"])
        if step:
            e["rendered_subject"] = _render_template(step["subject_template"], e)
            e["rendered_body"] = _render_template(step["body_template"], e)
        else:
            e["rendered_subject"] = None
            e["rendered_body"] = None
    return {"enrollments": enrollments}


@app.post("/api/sequences/auto-enroll-by-persona")
def api_auto_enroll_by_persona(account: dict = Depends(get_current_account)):
    """Fase 3: schrijft in één keer elk contact met een buyer persona (dat nog
    nergens actief loopt) in op de actieve sequence die aan diezelfde persona
    gekoppeld is. Zie database.auto_enroll_by_persona()."""
    return {"success": True, **database.auto_enroll_by_persona(account["id"])}


# Tussenoplossing (zie crm-roadmap.md, "verzenddagen en verzendtijdstip
# instellen" - nog niet gescoped als volwaardige instelling): spreidt
# automatische verzending vanuit de cron-endpoints hieronder random uit
# tussen 08:00-09:30 Amsterdam-tijd, i.p.v. alles in één klap te versturen
# zodra de cron due-items tegenkomt - dat oogt minder als een
# geautomatiseerde blast richting de ontvanger. Geldt niet voor een
# handmatige "Campagne lanceren"-klik (api_launch_campaign) - dat is een
# expliciete gebruikersactie, geen automatische periodieke verzending.
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")
SEND_WINDOW_START_MINUTE = 8 * 60       # 08:00
SEND_WINDOW_END_MINUTE = 9 * 60 + 30    # 09:30


@functools.lru_cache(maxsize=None)
def _nl_holidays(year: int) -> frozenset:
    """Nederlandse nationale feestdagen (de dagen die de Algemene
    termijnenwet erkent) voor een gegeven jaar. Paasgebonden dagen via de
    Anonieme Gregoriaanse paasformule (Meeus/Jones/Butcher) - geen externe
    dependency nodig voor zo'n kleine, jaarlijks terugkerende berekening.
    Eerste Paas-/Pinksterdag vallen altijd op zondag (al een uitgesloten
    dag via send_days) en staan er voor de volledigheid toch in."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    easter = date(year, month, day + 1)
    koningsdag = date(year, 4, 27)
    if koningsdag.isoweekday() == 7:  # zondag -> een dag eerder
        koningsdag = date(year, 4, 26)
    return frozenset({
        date(year, 1, 1),                       # Nieuwjaarsdag
        easter - timedelta(days=2),              # Goede Vrijdag
        easter,                                   # Eerste Paasdag
        easter + timedelta(days=1),               # Tweede Paasdag
        koningsdag,                               # Koningsdag
        date(year, 5, 5),                         # Bevrijdingsdag
        easter + timedelta(days=39),              # Hemelvaartsdag
        easter + timedelta(days=49),              # Eerste Pinksterdag
        easter + timedelta(days=50),              # Tweede Pinksterdag
        date(year, 12, 25),                       # Eerste Kerstdag
        date(year, 12, 26),                       # Tweede Kerstdag
    })


def _in_send_window(item_key, account_id, schedule_cache: dict) -> bool:
    """Elk item (sequence-enrollment/campaign-recipient) krijgt een
    stabiel, deterministisch moment binnen het venster (afgeleid van zijn
    eigen id, dus hetzelfde resultaat bij elke cron-aanroep) en wordt pas
    verstuurd zodra de huidige tijd dat moment is gepasseerd. Buiten
    08:00-09:30 wordt nooit verstuurd, en ook niet op een dag die het
    account heeft uitgesloten (weekend/feestdag, zie send-schedule-
    settings) - het item blijft gewoon 'due' en wordt bij de eerstvolgende
    toegestane cron-run alsnog opgepakt (dezelfde 'blijft due totdat het
    kan'-aanpak als hieronder al voor het tijdvenster gold, nu ook voor
    dagen). schedule_cache is een dict die de caller per cron-run
    initialiseert, zodat de instellingen niet per item opnieuw uit de
    database hoeven te worden gehaald."""
    if account_id not in schedule_cache:
        schedule_cache[account_id] = database.get_send_schedule_settings(account_id)
    settings = schedule_cache[account_id]
    now = datetime.now(AMSTERDAM_TZ)
    allowed_days = {int(d) for d in settings["send_days"].split(",") if d}
    if now.isoweekday() not in allowed_days:
        return False
    if settings["send_exclude_holidays_nl"] and now.date() in _nl_holidays(now.year):
        return False
    minute_of_day = now.hour * 60 + now.minute
    if not (SEND_WINDOW_START_MINUTE <= minute_of_day <= SEND_WINDOW_END_MINUTE):
        return False
    window_length = SEND_WINDOW_END_MINUTE - SEND_WINDOW_START_MINUTE
    target_offset = random.Random(item_key).randint(0, window_length)
    return minute_of_day >= SEND_WINDOW_START_MINUTE + target_offset


@app.post("/api/cron/process-sequences", dependencies=[Depends(require_admin_secret)])
def api_process_sequences():
    """Verstuurt elke vervallen sequence-stap, over ALLE accounts heen - dus
    beveiligd met dezelfde X-Admin-Secret als de andere cross-account
    beheer-endpoints, niet met een account-sessie. Bedoeld om periodiek
    aangeroepen te worden (bv. een uur-cron op Render of een externe
    scheduler) - zie DEPLOY.md."""
    database.record_cron_run("process-sequences")
    processed, skipped, errors, throttled, waiting_for_window, blocked_bad_name, blocked_deleted = 0, 0, 0, 0, 0, 0, 0
    schedule_cache: dict = {}
    for enrollment in database.due_enrollments():
        account_id = enrollment["seq_account_id"]
        if enrollment.get("deleted_at"):
            # Geen skip_enrollment (permanent) - een verwijderd contact kan
            # hersteld worden (zie POST /api/contacts/{id}/restore), en dan
            # moet de sequence gewoon verdergaan. Blijft dus "due" en wordt
            # vanzelf weer opgepakt zodra het contact hersteld is (of
            # verdwijnt vanzelf uit due_enrollments zodra het definitief
            # verwijderd wordt, via de cascade in delete_contact()).
            blocked_deleted += 1
            continue
        if enrollment["do_not_contact"] or enrollment["excluded_reason"]:
            database.skip_enrollment(enrollment["id"], "Contact is niet meer te benaderen of uitgesloten.")
            skipped += 1
            continue
        if not _in_send_window(f"seq:{enrollment['id']}", account_id, schedule_cache):
            waiting_for_window += 1
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
        if not _looks_like_real_name(enrollment["first_name"]):
            # Geen skip_enrollment (permanent) - blijft "due" en wordt
            # vanzelf weer opgepakt zodra iemand de naam van dit contact
            # herstelt. Gevonden bij een echte verzending (15 sept 2026):
            # een contact zonder echte voornaam kreeg een kapotte aanhef
            # ("roy.janssen", het e-mailadres-lokale-deel) - liever
            # helemaal niet versturen dan zo'n mail de deur uit laten gaan.
            database.log_contact_activity(
                account_id, enrollment["contact_id"], "send_blocked_bad_name",
                "Automatische mail tegengehouden: geen bruikbare voornaam - vul een echte voornaam in om te hervatten.",
            )
            blocked_bad_name += 1
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
        subject = _plain_text_from_html(_render_template(step["subject_template"], contact))
        body = _plain_text_from_html(_render_template(step["body_template"], contact))
        body = _with_signature_plain(account_id, body)
        body = _with_unsubscribe_footer_plain(account_id, enrollment["contact_id"], body)
        try:
            _send_plain_for_account(
                account_id, enrollment["email"], subject, body, sender_user_id=enrollment.get("assigned_to"),
            )
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
    return {
        "success": True, "processed": processed, "skipped": skipped, "errors": errors,
        "throttled": throttled, "waiting_for_window": waiting_for_window, "blocked_bad_name": blocked_bad_name,
        "blocked_deleted": blocked_deleted,
    }


@app.post("/api/cron/process-campaign-queue", dependencies=[Depends(require_admin_secret)])
def api_process_campaign_queue():
    """Fase 3c: werkt de wachtrij weg die ontstaat wanneer een campagne-
    launch werd afgekapt door de dagelijkse verzendlimiet (domain warm-up,
    zie remaining_daily_budget() in database.py en api_launch_campaign
    hierboven) - per account tot maximaal het resterende dagbudget, oudste
    campagne/ontvanger eerst. Zelfde beveiliging/aanroeppatroon als
    POST /api/cron/process-sequences: bedoeld om periodiek (bv. elk uur)
    van buitenaf getriggerd te worden, zie DEPLOY.md."""
    database.record_cron_run("process-campaign-queue")
    sent, failed, throttled_accounts, waiting_for_window, blocked_bad_name = 0, 0, 0, 0, 0
    schedule_cache: dict = {}
    for account_id in database.account_ids_with_pending_campaign_sends():
        remaining_budget = database.remaining_daily_budget(account_id)
        if remaining_budget is not None and remaining_budget <= 0:
            throttled_accounts += 1
            continue
        take = remaining_budget if remaining_budget is not None else 1000
        for r in database.pending_campaign_recipients_for_account(account_id, take):
            if not _in_send_window(f"camp:{r['id']}", account_id, schedule_cache):
                waiting_for_window += 1
                continue
            result = _attempt_send_campaign_recipient(account_id, r)
            if result == "sent":
                sent += 1
            elif result == "blocked_bad_name":
                blocked_bad_name += 1
            else:
                failed += 1
    return {
        "success": True, "sent": sent, "failed": failed, "blocked_bad_name": blocked_bad_name,
        "throttled_accounts": throttled_accounts, "waiting_for_window": waiting_for_window,
    }


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
    database.record_cron_run("process-digests")
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


@app.post("/api/cron/process-flow-monitor", dependencies=[Depends(require_admin_secret)])
def api_process_flow_monitor():
    """"Flows die aandacht nodig hebben": evalueert per account (met
    flow_monitor_enabled=1) actieve sequences en gelanceerde campagnes met
    een openstaande verzendwachtrij tegen de ingestelde reply-rate-drempel,
    en pauzeert automatisch wat eronder zit - zie
    database.flag_underperforming_flows(). Zelfde beveiliging/
    aanroeppatroon als de andere periodieke cron-endpoints, zie DEPLOY.md."""
    database.record_cron_run("process-flow-monitor")
    accounts_checked, paused_total, errors = 0, 0, 0
    for account_id in database.account_ids_with_flow_monitor_enabled():
        try:
            result = database.flag_underperforming_flows(account_id)
            paused_total += len(result["paused_sequences"]) + len(result["paused_campaigns"])
            accounts_checked += 1
        except Exception as exc:  # noqa: BLE001 - één account-fout mag de hele cron-run niet stoppen
            logger.warning("Flow-monitor mislukt voor account %s: %s", account_id, exc)
            errors += 1
    return {"success": True, "accounts_checked": accounts_checked, "paused": paused_total, "errors": errors}


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
    sector: str = ''  # sector/branche-label op de campagne zelf, voor latere performance-vergelijking


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
def api_create_campaign(payload: CampaignIn, account: dict = Depends(require_admin_role)):
    aid = account["id"]
    if database.count_contacts(aid) == 0:
        raise HTTPException(
            status_code=400,
            detail="Geen contacten aanwezig. Voeg eerst contacten toe via POST /api/contacts voordat je een campagne maakt.",
        )
    variants = [v.model_dump() for v in payload.variants] if payload.variants else DEFAULT_VARIANTS
    result = database.create_campaign(
        aid, payload.name, variants, include_excluded=payload.include_excluded, only_persona_id=payload.persona_id,
        sector=payload.sector,
    )
    return {"success": True, **result}


class CampaignUpdateIn(BaseModel):
    sector: str | None = None
    persona_id: int | None = None
    clear_persona: bool = False


@app.put("/api/campaigns/{campaign_id}")
def api_update_campaign(campaign_id: int, payload: CampaignUpdateIn, account: dict = Depends(require_admin_role)):
    """Sector/persona op een al aangemaakte campagne bijwerken - ook voor
    campagnes die al 'launched' zijn (retroactief taggen zodat sector/
    persona-analyse ze kan meenemen), zie database.update_campaign."""
    result = database.update_campaign(
        campaign_id, account["id"], sector=payload.sector,
        persona_id=payload.persona_id, clear_persona=payload.clear_persona,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Campagne niet gevonden.")
    return {"success": True, "campaign": result}


class AddCampaignContactsIn(BaseModel):
    contact_ids: list[int]


@app.post("/api/campaigns/{campaign_id}/add-contacts")
def api_add_contacts_to_campaign(campaign_id: int, payload: AddCampaignContactsIn, account: dict = Depends(get_current_account)):
    """Contacten aan een bestaande campagne toevoegen (bv. na dagelijkse
    prospecting) - zie database.add_contacts_to_campaign voor de
    ronde-robin-verdeling en de bewuste afwijking t.o.v. create_campaign
    m.b.t. excluded_reason."""
    result = database.add_contacts_to_campaign(campaign_id, account["id"], payload.contact_ids)
    if result is None:
        raise HTTPException(status_code=404, detail="Campagne niet gevonden.")
    return {"success": True, **result}


def _render_template(template: str, contact: dict) -> str:
    return (
        template.replace("{{firstName}}", contact.get("first_name") or "")
        .replace("{{lastName}}", contact.get("last_name") or "")
        .replace("{{company}}", contact.get("company") or "")
    )


_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text_from_html(text: str) -> str:
    """Automatische sequence-stappen worden als platte tekst verstuurd (zie
    _send_plain_for_account) - maar AI-gegenereerde content (Email
    Generator, variant-suggesties) gebruikt <br><br> voor alinea's, wat
    anders letterlijk als "<br><br>" in de ontvangen mail verschijnt
    (gevonden bij een echte verzending, 15 sept 2026). Zet br-tags om naar
    echte regeleinden en strip verder alle andere HTML-tags defensief."""
    text = _BR_TAG_RE.sub("\n", text)
    return _HTML_TAG_RE.sub("", text)


_REAL_NAME_RE = re.compile(r"^[A-ZÀ-Ý][A-Za-zà-ÿÀ-Ý'’]*(?:[ \-][A-ZÀ-Ý][A-Za-zà-ÿÀ-Ý'’]*)*$")


def _looks_like_real_name(name: str) -> bool:
    """Voorkomt dat een automatische mail met een kapotte aanhef verstuurd
    wordt - bv. "roy.janssen" (het e-mailadres-lokale-deel, gebruikt als
    terugvaloptie bij een CSV-import zonder voornaam-kolom, gevonden bij
    een echte verzending 15 sept 2026). Een echte naam begint met een
    hoofdletter en bevat geen "@", cijfers of punten - een e-mailadres of
    e-mail-local-part komt hier dus nooit doorheen."""
    name = (name or "").strip()
    return bool(name) and bool(_REAL_NAME_RE.match(name))


def _attempt_send_campaign_recipient(aid: int, r: dict) -> str:
    """Rendert en verstuurt één campagne-ontvanger, en logt het resultaat
    (record_send_result) - gedeeld tussen api_launch_campaign (directe
    launch) en api_process_campaign_queue (het wegwerken van een wachtrij
    die is ontstaan doordat de dagelijkse verzendlimiet een launch afkapte,
    zie remaining_daily_budget()). Geeft "sent"/"failed"/"blocked_bad_name"
    terug - bij dat laatste wordt bewust NIET record_send_result
    aangeroepen, zodat cr.sent_at/send_error allebei leeg blijven en de
    ontvanger "pending" blijft staan (zelfde plek als de bestaande
    wachtrij-mechaniek) - zodra iemand de naam van dit contact herstelt,
    pakt de eerstvolgende cron-run 'm vanzelf weer op."""
    if not _looks_like_real_name(r.get("first_name")):
        database.log_contact_activity(
            aid, r["contact_id"], "send_blocked_bad_name",
            "Automatische mail tegengehouden: geen bruikbare voornaam - vul een echte voornaam in om te hervatten.",
        )
        return "blocked_bad_name"
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
    full_html = _with_signature_html(aid, full_html)
    full_html = _with_unsubscribe_footer_html(aid, r["contact_id"], full_html)

    try:
        _send_html_for_account(aid, r["email"], subject, full_html, sender_user_id=r.get("assigned_to"))
        database.record_send_result(
            r["recipient_id"], sent=True, rendered_subject=subject, rendered_body=full_html,
        )
        return "sent"
    except Exception as exc:  # noqa: BLE001
        database.record_send_result(
            r["recipient_id"], sent=False, error=str(exc),
            rendered_subject=subject, rendered_body=full_html,
        )
        return "failed"


@app.post("/api/campaigns/{campaign_id}/launch")
def api_launch_campaign(campaign_id: int, account: dict = Depends(require_admin_role)):
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

    sent, failed, blocked_bad_name = 0, 0, 0
    for r in recipients:
        result = _attempt_send_campaign_recipient(aid, r)
        if result == "sent":
            sent += 1
        elif result == "blocked_bad_name":
            blocked_bad_name += 1
        else:
            failed += 1

    database.mark_campaign_launched(campaign_id)
    return {
        "success": True, "sent": sent, "failed": failed, "blocked_bad_name": blocked_bad_name,
        "queued": queued, "total": sent + failed + blocked_bad_name + queued,
    }


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


def _fallback_icp_suggestions(icp_data: dict) -> list:
    """Niet-AI terugvaloptie (geen ANTHROPIC_API_KEY, of de AI-aanroep
    faalde) - eenvoudige regelgebaseerde adviezen rechtstreeks uit
    icp_scores(), zelfde soort terugvalpatroon als de vaste 4
    lead-magnet-varianten bij api_suggest_campaign_variants hierboven."""
    suggestions = []
    rec = icp_data.get("recommended_icp")
    if rec and rec.get("basis") == "combinatie":
        suggestions.append(
            f"Focus op sector '{rec['sector']}', persona '{rec['persona']}' en omzetcategorie "
            f"'{rec['revenue_range']}' - dit is nu je best presterende combinatie "
            f"(score {rec['score']}/100, reply-rate {round(rec['reply_rate'] * 100, 1)}%, "
            f"gebaseerd op {rec['emails_sent']} verstuurde mails)."
        )
    elif rec and rec.get("basis") == "losse_dimensies":
        suggestions.append(
            "Nog geen enkele sector x persona x omzet-combinatie met genoeg data - verstuur meer "
            "campagnes/sequences naar dezelfde combinatie om een betrouwbaar advies te krijgen."
        )
    else:
        suggestions.append(
            "Nog onvoldoende verzend- en respons-data om een ICP-advies te geven - verstuur eerst meer "
            "campagnes of opvolgsequenties."
        )

    dq = icp_data.get("data_quality") or {}
    total = dq.get("contacts_total") or 0
    if total:
        missing_max = max(
            dq.get("contacts_missing_sector", 0),
            dq.get("contacts_missing_persona", 0),
            dq.get("contacts_missing_revenue_range", 0),
        )
        if missing_max / total > 0.2:
            suggestions.append(
                f"Vul sector, buyer persona en/of omzetcategorie in bij meer contacten (van de {total} "
                f"contacten mist dit nu bij een deel) - zonder die gegevens kan de ICP-analyse minder "
                f"combinaties meenemen."
            )
    return suggestions


def _generate_and_save_icp_suggestions(account_id: int) -> dict:
    """Gedeeld tussen de handmatige 'Genereer nu'-knop en de wekelijkse
    cron - berekent icp_scores(), probeert een AI-versie en valt terug op
    _fallback_icp_suggestions bij geen AI-koppeling of een mislukte
    aanroep, en slaat het resultaat op (overschrijft de vorige ronde)."""
    icp_data = database.icp_scores(account_id)
    source = "template"
    suggestions = None
    if ai_client.is_configured():
        try:
            suggestions = ai_client.generate_icp_suggestions(icp_data)
            source = "ai"
        except Exception as exc:  # noqa: BLE001 - val terug op de template-versie
            logger.warning("AI-ICP-suggesties genereren mislukt voor account %s: %s", account_id, exc)
    if not suggestions:
        suggestions = _fallback_icp_suggestions(icp_data)
    return database.save_icp_suggestions(account_id, suggestions, source)


@app.get("/api/analytics/icp-suggestions")
def api_get_icp_suggestions(account: dict = Depends(get_current_account)):
    return {"result": database.get_icp_suggestions(account["id"])}


@app.post("/api/analytics/icp-suggestions/generate")
def api_generate_icp_suggestions_now(account: dict = Depends(get_current_account)):
    """Handmatige trigger (bv. bij het eerste bezoek, voordat de wekelijkse
    cron ooit is gedraaid) - zelfde onderliggende logica als de cron."""
    return {"success": True, "result": _generate_and_save_icp_suggestions(account["id"])}


@app.post("/api/cron/process-icp-suggestions", dependencies=[Depends(require_admin_secret)])
def api_process_icp_suggestions():
    """Wekelijkse, automatisch gegenereerde ICP-verbetervoorstellen
    (crm-roadmap.md) - zelfde beveiligings-/foutisolatiepatroon als de
    andere cron-endpoints. accounts_needing_icp_suggestions() is
    idempotent per periode (standaard 7 dagen), dus vaker draaien dan
    nodig is onschadelijk."""
    database.record_cron_run("process-icp-suggestions")
    processed, errors = 0, 0
    for account_id in database.accounts_needing_icp_suggestions():
        try:
            _generate_and_save_icp_suggestions(account_id)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - één account-fout mag de hele cron-run niet stoppen
            logger.warning("ICP-suggesties genereren mislukt voor account %s: %s", account_id, exc)
            errors += 1
    return {"success": True, "processed": processed, "errors": errors}


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


class LinkedinFollowupSettingsIn(BaseModel):
    linkedin_followup_reminder_enabled: bool | None = None
    linkedin_followup_reminder_days: int | None = None


@app.get("/api/account/linkedin-followup-settings")
def api_get_linkedin_followup_settings(account: dict = Depends(get_current_account)):
    return database.get_linkedin_followup_settings(account["id"])


@app.put("/api/account/linkedin-followup-settings")
def api_update_linkedin_followup_settings(payload: LinkedinFollowupSettingsIn, account: dict = Depends(get_current_account)):
    if payload.linkedin_followup_reminder_days is not None and payload.linkedin_followup_reminder_days < 1:
        raise HTTPException(status_code=400, detail="Het aantal dagen moet minstens 1 zijn.")
    settings = database.update_linkedin_followup_settings(
        account["id"], linkedin_followup_reminder_enabled=payload.linkedin_followup_reminder_enabled,
        linkedin_followup_reminder_days=payload.linkedin_followup_reminder_days,
    )
    return {"success": True, **settings}


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
