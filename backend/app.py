"""
Twikey Sales Platform - backend.

A FastAPI service that gives the front-end dashboard real, working
functionality behind every tab, for multiple customer accounts:

  - Auth: email+password login, opaque session tokens (see auth.py/database.py).
    Every account only ever sees its own data - see the multi-tenancy note
    in database.py, including the one thing that is NOT tenant-isolated yet
    (email sending still goes through one shared Gmail mailbox).
  - Email Sync: send/read mail via Gmail API (SEND_AS_EMAIL, shared for now).
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

import html
import os
import urllib.parse

from dotenv import load_dotenv

# Load .env BEFORE importing any of our own modules - auth.py reads
# ADMIN_SECRET from the environment, and gmail_client.py/database.py read
# their own settings the same way. Importing them first and calling
# load_dotenv() after would mean any module-level `os.environ.get(...)` in
# those modules runs before .env has actually been loaded (only matters for
# local dev with a .env file; on Render the real env vars are already set
# before Python even starts, so this ordering doesn't affect production).
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

import database
from auth import get_current_account, require_admin_secret
from gmail_client import list_recent_messages, send_email, send_html_email
from validation import validate_message

SEND_AS_EMAIL = os.environ.get("SEND_AS_EMAIL", "sales@twikeycampaigns.nl")
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",")]
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Initialised at import time (not only on ASGI startup) so it also runs
# correctly under test runners/tools that call the app without going
# through a full server startup lifecycle.
database.init_db()


@app.on_event("startup")
def _startup():
    database.init_db()


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
    token = database.create_session(account["id"])
    return {"token": token, "account": account}


@app.post("/api/auth/logout")
def api_logout(account: dict = Depends(get_current_account), authorization: str = Header(default=None)):
    token = authorization.split(" ", 1)[1].strip()
    database.delete_session(token)
    return {"success": True}


@app.get("/api/auth/me")
def api_me(account: dict = Depends(get_current_account)):
    return {"account": account}


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
    existing = database.get_account_by_email(payload.login_email)
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
    Admin-only password reset. There is no self-service "forgot password"
    flow (that would need outbound reset-link emails, not built yet) - this
    is the practical way a forgotten password gets fixed for now: whoever
    holds ADMIN_SECRET resets it directly. Also invalidates that account's
    existing sessions.
    """
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minstens 8 tekens zijn.")
    found = database.set_password(payload.login_email, payload.new_password)
    if not found:
        raise HTTPException(status_code=404, detail="Geen account gevonden met dit e-mailadres.")
    return {"success": True}


# ---------------------------------------------------------------------------
# Email Sync (shared mailbox - see the multi-tenancy note in database.py)
# ---------------------------------------------------------------------------

class SendEmailRequest(BaseModel):
    to: EmailStr
    subject: str
    message: str


@app.post("/api/send")
def api_send_email(payload: SendEmailRequest, account: dict = Depends(get_current_account)):
    """Send an email via Gmail on behalf of SEND_AS_EMAIL."""
    try:
        result = send_email(SEND_AS_EMAIL, payload.to, payload.subject, payload.message)
    except Exception as exc:  # noqa: BLE001 - surface the real reason to the caller
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "message_id": result.get("id")}


@app.get("/api/inbox")
def api_inbox(max_results: int = 10, account: dict = Depends(get_current_account)):
    """Return the most recent inbox messages, unread count and today's count."""
    try:
        return list_recent_messages(SEND_AS_EMAIL, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

class ContactIn(BaseModel):
    first_name: str
    email: EmailStr
    last_name: str = ""
    company: str = ""
    linkedin_url: str = ""


@app.get("/api/contacts")
def api_list_contacts(account: dict = Depends(get_current_account)):
    aid = account["id"]
    return {"contacts": database.list_contacts(aid), "count": database.count_contacts(aid)}


@app.post("/api/contacts")
def api_add_contact(payload: ContactIn, account: dict = Depends(get_current_account)):
    contact = database.add_contact(
        account_id=account["id"],
        first_name=payload.first_name,
        email=payload.email,
        last_name=payload.last_name,
        company=payload.company,
        linkedin_url=payload.linkedin_url,
    )
    return {"success": True, "contact": contact}


class BulkContactsIn(BaseModel):
    contacts: list[ContactIn]


@app.post("/api/contacts/bulk")
def api_add_contacts_bulk(payload: BulkContactsIn, account: dict = Depends(get_current_account)):
    added = [
        database.add_contact(
            account_id=account["id"],
            first_name=c.first_name,
            email=c.email,
            last_name=c.last_name,
            company=c.company,
            linkedin_url=c.linkedin_url,
        )
        for c in payload.contacts
    ]
    return {"success": True, "added": len(added), "contacts": added}


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


class CampaignIn(BaseModel):
    name: str
    variants: list[VariantIn] | None = None  # omit to use the 4 default lead-magnet offers


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
    result = database.create_campaign(aid, payload.name, variants)
    return {"success": True, **result}


def _render_template(template: str, contact: dict) -> str:
    return (
        template.replace("{{firstName}}", contact.get("first_name") or "")
        .replace("{{lastName}}", contact.get("last_name") or "")
        .replace("{{company}}", contact.get("company") or "")
    )


@app.post("/api/campaigns/{campaign_id}/launch")
def api_launch_campaign(campaign_id: int, account: dict = Depends(get_current_account)):
    aid = account["id"]
    campaign = database.get_campaign(campaign_id, aid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne niet gevonden")

    recipients = database.campaign_recipients_for_launch(campaign_id, aid)
    if not recipients:
        raise HTTPException(status_code=400, detail="Geen ontvangers voor deze campagne (geen contacten aanwezig toen de campagne werd aangemaakt)")

    sent, failed = 0, 0
    for r in recipients:
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

        try:
            send_html_email(SEND_AS_EMAIL, r["email"], subject, full_html)
            database.record_send_result(r["recipient_id"], sent=True)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            database.record_send_result(r["recipient_id"], sent=False, error=str(exc))
            failed += 1

    database.mark_campaign_launched(campaign_id)
    return {"success": True, "sent": sent, "failed": failed, "total": len(recipients)}


@app.get("/api/campaigns/{campaign_id}/results")
def api_campaign_results(campaign_id: int, account: dict = Depends(get_current_account)):
    aid = account["id"]
    campaign = database.get_campaign(campaign_id, aid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne niet gevonden")
    return {"campaign": campaign["campaign"], "results": database.campaign_results(campaign_id, aid)}


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
