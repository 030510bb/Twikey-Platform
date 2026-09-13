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

import json
import os
import secrets
from collections import defaultdict
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

-- One row per account: lets that account send campaign/manual emails
-- through its own mailbox (their own domain, their own deliverability,
-- replies land in their own inbox) instead of the shared SEND_AS_EMAIL
-- Gmail sender. `password_encrypted` is never plaintext at rest - see
-- crypto.py - and is never returned by the API, only used server-side to
-- log in to the customer's SMTP server. Optional: an account with no row
-- here simply keeps using the shared sender, unchanged.
CREATE TABLE IF NOT EXISTS smtp_settings (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT NOT NULL,
    password_encrypted TEXT NOT NULL,
    from_email TEXT NOT NULL,
    from_name TEXT DEFAULT '',
    use_tls INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

-- Support/superadmin logins - deliberately separate from "users" above.
-- These aren't tied to any one customer account: they're Twikey staff who
-- can see across every account for support purposes. See the "Superadmin /
-- support" section further down for the account-overview/detail queries
-- and auth.py's get_current_admin for how these sessions are checked.
CREATE TABLE IF NOT EXISTS admins (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admins(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
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

-- Mini-CRM: a shared tag vocabulary per account (not per contact), so the
-- same tag ("warm lead", "beurs 2026", ...) can be reused/filtered across
-- contacts. contact_tags is the many-to-many join.
CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, name)
);

CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (contact_id, tag_id)
);

-- Fase 3: buyer personas. Net als tags een beheerde, gedeelde vocabulaire
-- per account, maar bewust GEEN many-to-many zoals tags: een contact heeft
-- op elk moment hoogstens één buyer persona (contacts.persona_id, zie
-- MIGRATIONS hieronder), zodat "de mail flow voor deze persona" ondubbelzinnig
-- is bij het kiezen van een sequence (sequences.persona_id) of
-- campagne-variant (campaign_variants.persona_id) - zie crm-roadmap.md Fase 3.
CREATE TABLE IF NOT EXISTS buyer_personas (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, name)
);

-- Fase 3b: bedrijfsprofiel per account ("intakeformulier", crm-roadmap.md
-- Fase 3). Eén rij per account - waardepropositie + USP's (één per regel,
-- zelfde simpele opslag-conventie als objection_templates.keywords) die
-- gebruikt worden om AI-mailsuggesties op maat te genereren i.p.v. de vaste
-- 4 lead-magnet varianten (zie ai_client.generate_variant_suggestions).
CREATE TABLE IF NOT EXISTS account_profiles (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    value_proposition TEXT NOT NULL DEFAULT '',
    usps TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Eén AI-verdiepingsronde (bewust geen doorlopend chatgesprek, zie
-- crm-roadmap.md Fase 3-scope-beslissing): bij het genereren van
-- verdiepende vragen slaat dit een klein aantal gerichte vervolgvragen op;
-- `answer` blijft NULL totdat de klant 'm invult. Een nieuwe ronde vervangen
-- (nog) onbeantwoorde vragen - beantwoorde vragen blijven bewaard als
-- geschiedenis, zie generate_profile_questions().
CREATE TABLE IF NOT EXISTS account_profile_questions (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    question TEXT NOT NULL,
    answer TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);

-- Fase 3c: intelligente CSV-import (crm-roadmap.md). Een tijdelijke sessie
-- tussen "preview" (headers + voorgestelde kolom-koppeling + een paar
-- voorbeeldrijen tonen) en "confirm" (daadwerkelijk importeren met de door
-- de klant goedgekeurde/aangepaste koppeling) - zodat een klant niet meer
-- handmatig kolomkoppen hoeft te hernoemen voordat een CSV geimporteerd kan
-- worden. `raw_headers`/`raw_rows`/`suggested_mapping` staan als JSON-tekst
-- (net als usps hierboven eenvoudige tekstopslag, hier JSON omdat het om
-- geneste structuren gaat). Geen cleanup-cron nodig: een verlopen/nooit
-- bevestigde sessie is gewoon een paar KB tekst die nooit meer opgehaald
-- wordt - zie POST /api/contacts/import-csv/preview en /confirm in app.py.
CREATE TABLE IF NOT EXISTS csv_import_sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL DEFAULT '',
    raw_headers TEXT NOT NULL,
    raw_rows TEXT NOT NULL,
    suggested_mapping TEXT NOT NULL,
    mapping_source TEXT NOT NULL DEFAULT 'rules',
    row_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Ideeenbus: feedback/ideeen van klanten, zodat die worden meegenomen in
-- toekomstige ontwikkeling i.p.v. alleen mondeling/losse berichten. Bewust
-- een aparte tabel van support_tickets hieronder - een supportvraag
-- verwacht een antwoord/oplossing, een ideeenbus-item is input voor de
-- roadmap en hoeft niet 1-op-1 beantwoord te worden (wel: status
-- bijhouden zodat Twikey kan laten zien wat ermee gebeurt).
CREATE TABLE IF NOT EXISTS feedback_items (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    user_id INTEGER REFERENCES users(id),
    category TEXT NOT NULL DEFAULT 'idee',
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'nieuw',
    created_at TEXT NOT NULL
);

-- Audit trail: one row per CRM-lifecycle event (created/imported, tag
-- added/removed, (re)assigned, marked excluded, note, etc). This is
-- deliberately NOT where campaign sends / opens / clicks / LinkedIn actions
-- live - those already exist in campaign_recipients and linkedin_outreach.
-- GET /api/contacts/{id}/timeline merges all three sources - see
-- contact_timeline() below - so "everything that happened to a lead,
-- including outgoing communication" is answered without duplicating data
-- that's already tracked elsewhere.
CREATE TABLE IF NOT EXISTS contact_activity (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    event_type TEXT NOT NULL,
    description TEXT NOT NULL,
    meta TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- Company/domain-level exclusion list (CSV-uploaded or added manually) -
-- distinct from per-contact is_customer/has_open_quote flags on contacts
-- itself: this one can block a company BEFORE any contact from it even
-- exists, e.g. importing a CSV of "existing customers" from another system.
CREATE TABLE IF NOT EXISTS exclusion_entries (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    domain TEXT NOT NULL,
    company_name TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    UNIQUE(account_id, domain)
);

-- "Agenderen": a reminder to reach back out to one contact on/after a given
-- date (e.g. "klant vroeg over 3 maanden terug te bellen"). due_reminders()
-- below lists what's due; completing one just sets status='done'.
CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    remind_at TEXT NOT NULL,
    note TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

-- Per-account "bring your own" credential for the Vibe Prospecting/Explorium
-- integration - same pattern as smtp_settings (crypto.py-encrypted at
-- rest). An account with no row here simply hasn't connected it yet; the
-- Integraties tab shows the button as "niet geconfigureerd" until it does.
-- No live API calls are made against this yet (Fase 2 in crm-roadmap.md) -
-- this table only stores the credential so the settings screen has
-- something real to save into.
CREATE TABLE IF NOT EXISTS prospecting_settings (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    api_key_encrypted TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Per-account HubSpot private-app token, used later (Fase 2) to live-check
-- whether a contact's company already is a HubSpot customer / has an open
-- deal, so prospecting can automatically skip them. Same
-- bring-your-own-credential pattern as smtp_settings/prospecting_settings.
CREATE TABLE IF NOT EXISTS hubspot_settings (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    access_token_encrypted TEXT NOT NULL,
    exclude_customers INTEGER NOT NULL DEFAULT 1,
    exclude_open_deals INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

-- Self-service knowledge base for the support page. Global (not per
-- account) - every customer sees the same FAQ/how-to articles.
CREATE TABLE IF NOT EXISTS kb_articles (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- A support question a customer submits when the FAQ doesn't answer it.
-- Visible to Twikey support in admin.html; status tracks the reply flow.
CREATE TABLE IF NOT EXISTS support_tickets (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    user_id INTEGER REFERENCES users(id),
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    admin_reply TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    replied_at TEXT
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

-- Fase 2: bezwaren-bibliotheek. Seeded per account (net als
-- linkedin_templates) uit DEFAULT_OBJECTIONS zodat elk account met iets
-- bruikbaars start; de klant kan zelf categorieen toevoegen/aanpassen.
-- `keywords` is een komma-gescheiden lijst die _categorize_reply gebruikt om
-- een binnenkomende reply automatisch aan een categorie te koppelen.
CREATE TABLE IF NOT EXISTS objection_templates (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    category TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    suggested_reply TEXT NOT NULL
);

-- Fase 2: een reply die via IMAP is binnengehaald (zie imap_client.py) en
-- (indien mogelijk) gekoppeld aan een contact + campagne. `message_uid` is
-- de IMAP UID van het bericht op het moment van ophalen - de UNIQUE-
-- constraint met account_id voorkomt dat hetzelfde bericht twee keer wordt
-- verwerkt als de mailbox opnieuw wordt gepolld.
CREATE TABLE IF NOT EXISTS incoming_replies (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    contact_id INTEGER REFERENCES contacts(id),
    campaign_id INTEGER REFERENCES campaigns(id),
    from_email TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    message_uid TEXT NOT NULL,
    objection_category TEXT DEFAULT '',
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, message_uid)
);

-- Fase 2: het conceptantwoord (AI of template-fallback) dat bij een
-- incoming_reply hoort. Blijft 'pending' totdat een gebruiker het
-- goedkeurt/verstuurt of afwijst - tenzij accounts.auto_reply_enabled aan
-- staat, dan wordt 'approved'+verstuurd direct doorlopen. Zie
-- crm-roadmap.md punt 6: nooit automatisch verzonden by default.
CREATE TABLE IF NOT EXISTS reply_drafts (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    incoming_reply_id INTEGER NOT NULL REFERENCES incoming_replies(id),
    draft_body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'template',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER REFERENCES users(id),
    sent_error TEXT
);

-- Fase 2: opvolgmail-sequenties (drip campaigns). Een sequence heeft
-- geordende stappen met een wachttijd; sequence_enrollments volgt de
-- voortgang per contact en stopt automatisch bij een reply (zie
-- crm-roadmap.md "Aanvullingen" - sequence stopt bij reply, i.p.v. nog een
-- opvolgmail te sturen).
CREATE TABLE IF NOT EXISTS sequences (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_steps (
    id SERIAL PRIMARY KEY,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id),
    step_order INTEGER NOT NULL,
    wait_days INTEGER NOT NULL DEFAULT 3,
    subject_template TEXT NOT NULL,
    body_template TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_enrollments (
    id SERIAL PRIMARY KEY,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    current_step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    next_send_at TEXT,
    enrolled_at TEXT NOT NULL,
    stopped_reason TEXT DEFAULT '',
    UNIQUE (sequence_id, contact_id)
);

CREATE TABLE IF NOT EXISTS sequence_sends (
    id SERIAL PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES sequence_enrollments(id),
    step_id INTEGER NOT NULL REFERENCES sequence_steps(id),
    sent_at TEXT,
    send_error TEXT
);

-- Email Generator: opgeslagen AI-gegenereerde cold-outreach e-mails per
-- account, aangemaakt via een handmatige "Opslaan" klik in de Email
-- Generator-tab. Puur een persoonlijke bibliotheek, zelfde
-- niet-automatische aanpak als objection_templates.
CREATE TABLE IF NOT EXISTS email_generator_templates (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    sector TEXT NOT NULL DEFAULT 'horeca',
    persona TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    angle TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# Columns added to tables that already existed in production before the
# mini-CRM feature set - CREATE TABLE IF NOT EXISTS above never touches an
# existing table's columns, so those need an explicit ALTER TABLE. Postgres
# supports "ADD COLUMN IF NOT EXISTS", so this is safe to run on every
# startup (init_db()), on a brand new database as well as an existing one.
MIGRATIONS = """
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS job_title TEXT DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS sector TEXT DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS assigned_to INTEGER REFERENCES users(id);
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS company_domain TEXT DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS is_customer INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS has_open_quote INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS do_not_contact INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS excluded_reason TEXT DEFAULT '';

ALTER TABLE smtp_settings ADD COLUMN IF NOT EXISTS imap_host TEXT;
ALTER TABLE smtp_settings ADD COLUMN IF NOT EXISTS imap_port INTEGER;
ALTER TABLE smtp_settings ADD COLUMN IF NOT EXISTS imap_username TEXT;
ALTER TABLE smtp_settings ADD COLUMN IF NOT EXISTS imap_password_encrypted TEXT;
ALTER TABLE smtp_settings ADD COLUMN IF NOT EXISTS imap_use_ssl INTEGER NOT NULL DEFAULT 1;

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS auto_reply_enabled INTEGER NOT NULL DEFAULT 0;

-- Fase 2: onthoudt de hoogste IMAP UID die al verwerkt is per account, zodat
-- elke poll alleen naar nieuwe berichten hoeft te zoeken (SEARCH UID > x) in
-- plaats van de hele mailbox opnieuw te doorzoeken.
ALTER TABLE smtp_settings ADD COLUMN IF NOT EXISTS imap_last_uid INTEGER NOT NULL DEFAULT 0;

-- Fase 3: buyer personas - één optionele persona per contact (zie
-- buyer_personas hierboven in SCHEMA), en een optionele persona-koppeling op
-- een opvolgsequence resp. een campagne-variant, zodat elke buyer persona
-- zijn eigen mail flow kan hebben. NULL betekent "geen specifieke persona" -
-- zo'n sequence/variant blijft de generieke fallback voor contacten zonder
-- (matchende) persona.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS persona_id INTEGER REFERENCES buyer_personas(id);
ALTER TABLE sequences ADD COLUMN IF NOT EXISTS persona_id INTEGER REFERENCES buyer_personas(id);
ALTER TABLE campaign_variants ADD COLUMN IF NOT EXISTS persona_id INTEGER REFERENCES buyer_personas(id);

-- Fase 3: audit trail met mailinhoud. Vanaf nu wordt de daadwerkelijk
-- verstuurde (gepersonaliseerde) tekst opgeslagen op het moment van
-- verzenden, i.p.v. alleen een verwijzing naar het sjabloon. Oudere rijen
-- houden deze kolommen leeg - de timeline valt dan terug op het sjabloon
-- zoals dat nu is (zie contact_timeline()).
ALTER TABLE campaign_recipients ADD COLUMN IF NOT EXISTS rendered_subject TEXT;
ALTER TABLE campaign_recipients ADD COLUMN IF NOT EXISTS rendered_body TEXT;
ALTER TABLE sequence_sends ADD COLUMN IF NOT EXISTS rendered_subject TEXT;
ALTER TABLE sequence_sends ADD COLUMN IF NOT EXISTS rendered_body TEXT;

-- sequence_sends.sent_at is only set on SUCCESS, so a failed attempt had no
-- timestamp at all and could never show up in contact_timeline() (which
-- drops any event without one). attempted_at is set on every attempt,
-- success or failure, and is what the timeline falls back to for failures.
ALTER TABLE sequence_sends ADD COLUMN IF NOT EXISTS attempted_at TEXT;

-- Fase 3b: optionele omschrijving per buyer persona (pijnpunten/context,
-- "buyer personas" uit het intakeformulier) - meegegeven aan Claude bij het
-- genereren van variant-suggesties zodat die persona-specifiek zijn, niet
-- alleen gebaseerd op de algemene waardepropositie.
ALTER TABLE buyer_personas ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';

-- Fase 3c: omzetcategorie van het bedrijf van dit contact (vrije tekst,
-- zelfde stijl als sector - bv. "0-1M", "1-10M", "10-50M", "50M+"). Samen
-- met sector en buyer persona de derde dimensie voor ICP-scoring (zie
-- icp_scores() hieronder): welke combinatie van sector x omzet x persona
-- de beste open/click/reply-resultaten oplevert.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS revenue_range TEXT DEFAULT '';

-- Fase 3c: dagelijkse samenvatting-mail. Standaard AAN (opt-out) - stuurt
-- elke dag hooguit één mail per account; last_digest_sent_date voorkomt
-- dubbel versturen als de cron vaker dan eens per dag draait (zie
-- POST /api/cron/process-digests).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS daily_digest_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_digest_sent_date TEXT;

-- Fase 3c: domain warm-up - instelbare dagelijkse verzendlimiet per account.
-- Standaard UIT (expliciete keuze van Benjamin: alleen accounts die zelf met
-- een nieuw domein starten zetten 'm aan, met hun eigen gekozen aantal) -
-- zie remaining_daily_budget() hieronder en de afdwinging in app.py
-- (campagne-launch + de sequence-cron).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS daily_send_limit_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS daily_send_limit INTEGER NOT NULL DEFAULT 50;

-- Fase 3c: afmeldlink in uitgaande mails (huisregel) - standaard AAN, maar
-- per account uit te zetten (zie sending-settings) voor wie bewust zonder
-- wil mailen. Een geldig afmeldverzoek zet altijd het bestaande
-- do_not_contact op de contactpersoon (geen apart "unsubscribed"-veld -
-- hergebruikt dezelfde, overal al gerespecteerde stop-vlag).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS unsubscribe_link_enabled INTEGER NOT NULL DEFAULT 1;
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

# Fase 2: standaard bezwaren-bibliotheek, geseed per account (zelfde patroon
# als DEFAULT_LINKEDIN_TEMPLATES hierboven). `keywords` wordt gebruikt door
# _categorize_reply (app.py) om een binnenkomende reply-tekst automatisch aan
# een categorie te koppelen - simpele keyword-matching, geen ML: voorspelbaar
# en uitlegbaar, en werkt zonder externe afhankelheid als er nog geen
# Anthropic-key is ingesteld.
DEFAULT_OBJECTIONS = [
    (
        "Geen tijd / geen prioriteit",
        "geen tijd,druk,later,geen prioriteit,niet nu,volgende kwartaal",
        "Begrijpelijk dat timing lastig is. Zou het helpen als ik over een paar maanden "
        "opnieuw contact opneem, of is het handiger als ik nu alvast kort iets stuur "
        "zodat je het kunt bekijken wanneer het uitkomt?",
    ),
    (
        "Al een oplossing / tevreden met huidige leverancier",
        "hebben al,gebruiken al,huidige leverancier,tevreden met,geen behoefte",
        "Fijn om te horen dat jullie al iets hebben ingericht. Veel van onze klanten "
        "gebruikten daarvoor ook al een andere oplossing - meestal ging het gesprek "
        "toen over [specifiek verschil]. Zou een korte vergelijking nuttig zijn, "
        "vrijblijvend?",
    ),
    (
        "Prijs / budget",
        "te duur,prijs,budget,kosten,geen geld",
        "Helder, budget is altijd een reële afweging. Kun je aangeven wat voor jullie "
        "een reeel budget zou zijn? Dan kan ik kijken of er een passende aanpak is, "
        "of anders eerlijk aangeven dat het nu niet aansluit.",
    ),
    (
        "Niet de juiste persoon / andere beslisser",
        "niet de juiste persoon,niet mijn afdeling,andere afdeling,collega",
        "Dank voor het doorgeven! Zou je kunnen aangeven wie dit binnen jullie "
        "organisatie wel behandelt, of vind je het goed als ik rechtstreeks contact "
        "opneem?",
    ),
    (
        "Geen interesse",
        "geen interesse,niet interessant,nee bedankt,niet relevant",
        "Duidelijk, dank voor de eerlijke reactie. Ik laat het hierbij - mocht de "
        "situatie ooit veranderen, dan hoor ik het graag.",
    ),
    (
        "Vraagt om meer informatie",
        "meer informatie,vertel meer,hoe werkt,kun je uitleggen,interessant",
        "Fijn dat je interesse hebt! Ik stuur je graag meer informatie toe - zullen we "
        "daarvoor kort bellen, of heb je liever eerst iets op papier?",
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
    """Create tables if they don't exist yet, then apply column migrations for
    tables that already existed before those columns were added. Does NOT
    seed any account - see create_account()."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(MIGRATIONS)
        _seed_default_kb_articles(conn)


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
        conn.executemany(
            "INSERT INTO objection_templates (account_id, category, keywords, suggested_reply) VALUES (?, ?, ?, ?)",
            [(account_id, category, keywords, reply) for category, keywords, reply in DEFAULT_OBJECTIONS],
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
# Email settings (per-account SMTP - see the smtp_settings table comment)
# ---------------------------------------------------------------------------

def get_smtp_settings(account_id: int):
    """The account's own SMTP config, or None if it hasn't set one up (in
    which case the caller should fall back to the shared SEND_AS_EMAIL
    sender). `password_encrypted` is only ever decrypted right before
    logging in to the SMTP server - see crypto.py / smtp_client.py - never
    exposed through the API."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM smtp_settings WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return dict(row) if row else None


def save_smtp_settings(
    account_id: int,
    host: str,
    port: int,
    username: str,
    password_encrypted: str,
    from_email: str,
    from_name: str,
    use_tls: bool,
    imap_host: str = None,
    imap_port: int = None,
    imap_username: str = None,
    imap_password_encrypted: str = None,
    imap_use_ssl: bool = True,
) -> dict:
    """Upsert - an account has at most one mail config, so saving again just
    replaces it (e.g. to rotate a password or switch provider). The imap_*
    fields are optional: an account can send via its own SMTP without also
    letting the platform read its inbox for replies - IMAP is only needed
    for the reply-tracking feature (see crm-roadmap.md, Fase 2)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO smtp_settings
                (account_id, host, port, username, password_encrypted, from_email, from_name, use_tls,
                 imap_host, imap_port, imap_username, imap_password_encrypted, imap_use_ssl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET
                host = EXCLUDED.host,
                port = EXCLUDED.port,
                username = EXCLUDED.username,
                password_encrypted = EXCLUDED.password_encrypted,
                from_email = EXCLUDED.from_email,
                from_name = EXCLUDED.from_name,
                use_tls = EXCLUDED.use_tls,
                imap_host = EXCLUDED.imap_host,
                imap_port = EXCLUDED.imap_port,
                imap_username = EXCLUDED.imap_username,
                imap_password_encrypted = EXCLUDED.imap_password_encrypted,
                imap_use_ssl = EXCLUDED.imap_use_ssl,
                updated_at = EXCLUDED.updated_at
            """,
            (
                account_id, host, port, username, password_encrypted, from_email, from_name, 1 if use_tls else 0,
                imap_host, imap_port, imap_username, imap_password_encrypted, 1 if imap_use_ssl else 0, now_iso(),
            ),
        )
        row = conn.execute("SELECT * FROM smtp_settings WHERE account_id = ?", (account_id,)).fetchone()
        return dict(row)


def delete_smtp_settings(account_id: int) -> bool:
    """Remove the account's custom SMTP config - it reverts to the shared
    sender immediately. Returns False if there was nothing to delete."""
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM smtp_settings WHERE account_id = ?", (account_id,)).fetchone()
        if not existing:
            return False
        conn.execute("DELETE FROM smtp_settings WHERE account_id = ?", (account_id,))
        return True


# ---------------------------------------------------------------------------
# Contacts / mini-CRM (scoped per account)
# ---------------------------------------------------------------------------

def _domain_from_email(email: str) -> str:
    return email.split("@", 1)[1].lower() if email and "@" in email else ""


def add_contact(
    account_id: int,
    first_name: str,
    email: str,
    last_name: str = "",
    company: str = "",
    linkedin_url: str = "",
    job_title: str = "",
    sector: str = "",
    revenue_range: str = "",
    source: str = "manual",
    _conn=None,
) -> dict:
    domain = _domain_from_email(email)

    def _run(conn):
        conn.execute(
            """
            INSERT INTO contacts (account_id, first_name, last_name, email, company, linkedin_url,
                                   job_title, sector, revenue_range, source, company_domain, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, email) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                company=excluded.company,
                linkedin_url=CASE WHEN excluded.linkedin_url = '' THEN contacts.linkedin_url ELSE excluded.linkedin_url END,
                job_title=CASE WHEN excluded.job_title = '' THEN contacts.job_title ELSE excluded.job_title END,
                sector=CASE WHEN excluded.sector = '' THEN contacts.sector ELSE excluded.sector END,
                revenue_range=CASE WHEN excluded.revenue_range = '' THEN contacts.revenue_range ELSE excluded.revenue_range END,
                company_domain=excluded.company_domain
            """,
            (account_id, first_name, last_name, email, company, linkedin_url,
             job_title, sector, revenue_range, source, domain, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? AND email = ?", (account_id, email)
        ).fetchone()
        contact = dict(row)
        exclusion = check_exclusion(account_id, domain, _conn=conn) if domain else None
        if exclusion and not contact.get("excluded_reason"):
            conn.execute(
                "UPDATE contacts SET excluded_reason = ? WHERE id = ?", (exclusion, contact["id"])
            )
            contact["excluded_reason"] = exclusion
        return contact

    if _conn is not None:
        return _run(_conn)
    with get_conn() as conn:
        result = _run(conn)
        log_contact_activity(account_id, result["id"], "created", f"Contact aangemaakt (bron: {source})", _conn=conn)
        return result


def list_contacts(account_id: int, q: str = None, tag: str = None, persona_id: int = None, assigned_to=None,
                   exclude_excluded: bool = False, exclude_dnc: bool = False) -> list:
    """List contacts for one account, newest first, each with its tags (list
    of {id, name}) and assignee (id/email or None) attached. Optional filters:
    q (matches first/last name, email or company, case-insensitive substring),
    tag (tag name), persona_id (buyer persona id), assigned_to (user id, or the string "none" for
    unassigned), exclude_excluded (drop contacts with a non-empty
    excluded_reason - e.g. before building a campaign), exclude_dnc (drop
    contacts marked "niet meer benaderen")."""
    with get_conn() as conn:
        sql = """
            SELECT c.*, u.email AS assigned_to_email, bp.name AS persona_name
            FROM contacts c
            LEFT JOIN users u ON u.id = c.assigned_to
            LEFT JOIN buyer_personas bp ON bp.id = c.persona_id
            WHERE c.account_id = ?
        """
        params = [account_id]
        if q:
            sql += """ AND (
                LOWER(c.first_name) LIKE ? OR LOWER(c.last_name) LIKE ?
                OR LOWER(c.email) LIKE ? OR LOWER(c.company) LIKE ?
            )"""
            like = f"%{q.lower()}%"
            params += [like, like, like, like]
        if persona_id is not None:
            sql += " AND c.persona_id = ?"
            params.append(persona_id)
        if assigned_to == "none":
            sql += " AND c.assigned_to IS NULL"
        elif assigned_to is not None:
            sql += " AND c.assigned_to = ?"
            params.append(assigned_to)
        if exclude_excluded:
            sql += " AND (c.excluded_reason IS NULL OR c.excluded_reason = '')"
        if exclude_dnc:
            sql += " AND c.do_not_contact = 0"
        if tag:
            sql += """ AND c.id IN (
                SELECT ct.contact_id FROM contact_tags ct
                JOIN tags t ON t.id = ct.tag_id
                WHERE t.account_id = ? AND t.name = ?
            )"""
            params += [account_id, tag]
        sql += " ORDER BY c.created_at DESC"

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if not rows:
            return rows

        tag_rows = conn.execute(
            """
            SELECT ct.contact_id, t.id AS tag_id, t.name
            FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id
            WHERE ct.contact_id = ANY(?)
            """,
            ([r["id"] for r in rows],),
        ).fetchall()
        tags_by_contact = {}
        for tr in tag_rows:
            tags_by_contact.setdefault(tr["contact_id"], []).append({"id": tr["tag_id"], "name": tr["name"]})
        for r in rows:
            r["tags"] = tags_by_contact.get(r["id"], [])
        return rows


def get_contact(contact_id: int, account_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT c.*, u.email AS assigned_to_email, bp.name AS persona_name
            FROM contacts c
            LEFT JOIN users u ON u.id = c.assigned_to
            LEFT JOIN buyer_personas bp ON bp.id = c.persona_id
            WHERE c.id = ? AND c.account_id = ?
            """,
            (contact_id, account_id),
        ).fetchone()
        if not row:
            return None
        contact = dict(row)
        tag_rows = conn.execute(
            "SELECT t.id, t.name FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id WHERE ct.contact_id = ?",
            (contact_id,),
        ).fetchall()
        contact["tags"] = [dict(t) for t in tag_rows]
        return contact


def update_contact(contact_id: int, account_id: int, **fields) -> dict:
    """Generic per-field updater for the CRM fields that aren't tags/assignment
    (those have their own dedicated functions below). Accepts any of:
    job_title, sector, revenue_range, company, linkedin_url, is_customer,
    has_open_quote, do_not_contact. Logs one contact_activity row summarising
    what changed."""
    allowed = {"job_title", "sector", "revenue_range", "company", "linkedin_url", "is_customer", "has_open_quote", "do_not_contact"}
    bool_fields = {"is_customer", "has_open_quote", "do_not_contact"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    updates = {k: (1 if v else 0) if k in bool_fields else v for k, v in updates.items()}
    if not updates:
        return get_contact(contact_id, account_id)
    with get_conn() as conn:
        owned = conn.execute(
            "SELECT * FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)
        ).fetchone()
        if not owned:
            return None
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [contact_id]
        conn.execute(f"UPDATE contacts SET {set_clause} WHERE id = ?", params)

        # Recompute excluded_reason for the manual flags - CSV/HubSpot-derived
        # reasons (see check_exclusion) take precedence and aren't cleared here.
        if "is_customer" in updates or "has_open_quote" in updates:
            new_row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            reason = new_row["excluded_reason"] or ""
            if updates.get("is_customer"):
                reason = "manual_customer"
            elif updates.get("has_open_quote"):
                reason = "manual_open_quote"
            elif reason in ("manual_customer", "manual_open_quote"):
                reason = ""
            conn.execute("UPDATE contacts SET excluded_reason = ? WHERE id = ?", (reason, contact_id))

        changed = ", ".join(f"{k}={v}" for k, v in updates.items())
        log_contact_activity(account_id, contact_id, "updated", f"Bijgewerkt: {changed}", _conn=conn)
        row = conn.execute(
            "SELECT c.*, u.email AS assigned_to_email FROM contacts c LEFT JOIN users u ON u.id = c.assigned_to WHERE c.id = ?",
            (contact_id,),
        ).fetchone()
        return dict(row)


def count_contacts(account_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE account_id = ?", (account_id,)
        ).fetchone()
        return row["n"]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def get_or_create_tag(account_id: int, name: str, _conn=None) -> dict:
    name = name.strip()

    def _run(conn):
        conn.execute(
            "INSERT INTO tags (account_id, name, created_at) VALUES (?, ?, ?) ON CONFLICT (account_id, name) DO NOTHING",
            (account_id, name, now_iso()),
        )
        row = conn.execute("SELECT * FROM tags WHERE account_id = ? AND name = ?", (account_id, name)).fetchone()
        return dict(row)

    if _conn is not None:
        return _run(_conn)
    with get_conn() as conn:
        return _run(conn)


def list_tags(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tags WHERE account_id = ? ORDER BY name ASC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_tag(tag_id: int, account_id: int) -> bool:
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM tags WHERE id = ? AND account_id = ?", (tag_id, account_id)).fetchone()
        if not owned:
            return False
        conn.execute("DELETE FROM contact_tags WHERE tag_id = ?", (tag_id,))
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return True


def add_tag_to_contact(contact_id: int, account_id: int, tag_name: str) -> dict:
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)).fetchone()
        if not owned:
            return None
        tag = get_or_create_tag(account_id, tag_name, _conn=conn)
        conn.execute(
            "INSERT INTO contact_tags (contact_id, tag_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (contact_id, tag["id"]),
        )
        log_contact_activity(account_id, contact_id, "tag_added", f"Tag toegevoegd: {tag['name']}", _conn=conn)
        return tag


def remove_tag_from_contact(contact_id: int, account_id: int, tag_id: int) -> bool:
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)).fetchone()
        if not owned:
            return False
        tag = conn.execute("SELECT name FROM tags WHERE id = ? AND account_id = ?", (tag_id, account_id)).fetchone()
        conn.execute("DELETE FROM contact_tags WHERE contact_id = ? AND tag_id = ?", (contact_id, tag_id))
        if tag:
            log_contact_activity(account_id, contact_id, "tag_removed", f"Tag verwijderd: {tag['name']}", _conn=conn)
        return True


# ---------------------------------------------------------------------------
# Buyer personas (Fase 3, crm-roadmap.md)
#
# A managed, per-account vocabulary like tags, but a contact has AT MOST ONE
# persona at a time (contacts.persona_id) rather than a many-to-many join -
# so "which mail flow does this contact get" (sequences.persona_id,
# campaign_variants.persona_id) is always unambiguous.
# ---------------------------------------------------------------------------

def create_buyer_persona(account_id: int, name: str) -> dict:
    name = name.strip()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO buyer_personas (account_id, name, created_at) VALUES (?, ?, ?) ON CONFLICT (account_id, name) DO NOTHING",
            (account_id, name, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM buyer_personas WHERE account_id = ? AND name = ?", (account_id, name)
        ).fetchone()
        return dict(row)


def list_buyer_personas(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM buyer_personas WHERE account_id = ? ORDER BY name ASC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_buyer_persona(persona_id: int, account_id: int) -> bool:
    """Deletes the persona and clears it from any contact/sequence/campaign
    variant still pointing at it (rather than blocking on the FK) - those
    simply fall back to "geen specifieke persona" behaviour afterwards."""
    with get_conn() as conn:
        owned = conn.execute(
            "SELECT 1 FROM buyer_personas WHERE id = ? AND account_id = ?", (persona_id, account_id)
        ).fetchone()
        if not owned:
            return False
        conn.execute("UPDATE contacts SET persona_id = NULL WHERE persona_id = ?", (persona_id,))
        conn.execute("UPDATE sequences SET persona_id = NULL WHERE persona_id = ?", (persona_id,))
        conn.execute("UPDATE campaign_variants SET persona_id = NULL WHERE persona_id = ?", (persona_id,))
        conn.execute("DELETE FROM buyer_personas WHERE id = ?", (persona_id,))
        return True


def set_contact_persona(contact_id: int, account_id: int, persona_id) -> dict:
    """Sets (or clears, with persona_id=None) a contact's single buyer
    persona. Returns the updated contact, or None if the contact (or a
    non-null persona_id) doesn't belong to this account."""
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)).fetchone()
        if not owned:
            return None
        persona_name = None
        if persona_id is not None:
            persona = conn.execute(
                "SELECT name FROM buyer_personas WHERE id = ? AND account_id = ?", (persona_id, account_id)
            ).fetchone()
            if not persona:
                return None
            persona_name = persona["name"]
        conn.execute("UPDATE contacts SET persona_id = ? WHERE id = ?", (persona_id, contact_id))
        log_contact_activity(
            account_id, contact_id, "persona_set",
            f"Buyer persona ingesteld: {persona_name}" if persona_name else "Buyer persona verwijderd",
            _conn=conn,
        )
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row)


def update_buyer_persona(persona_id: int, account_id: int, name: str = None, description: str = None) -> dict:
    """Fase 3b: past naam en/of omschrijving (pijnpunten/context) van een
    bestaande persona aan. Alleen meegegeven velden worden gewijzigd."""
    with get_conn() as conn:
        owned = conn.execute(
            "SELECT * FROM buyer_personas WHERE id = ? AND account_id = ?", (persona_id, account_id)
        ).fetchone()
        if not owned:
            return None
        new_name = name.strip() if name is not None else owned["name"]
        new_description = description if description is not None else owned["description"]
        conn.execute(
            "UPDATE buyer_personas SET name = ?, description = ? WHERE id = ?",
            (new_name, new_description, persona_id),
        )
        row = conn.execute("SELECT * FROM buyer_personas WHERE id = ?", (persona_id,)).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Bedrijfsprofiel / intake (Fase 3b, crm-roadmap.md) - waardepropositie +
# USP's per account, plus één AI-verdiepingsronde, gebruikt om
# AI-mailsuggesties op maat te genereren (zie ai_client.py en
# POST /api/campaigns/suggest-variants in app.py).
# ---------------------------------------------------------------------------

def get_account_profile(account_id: int) -> dict:
    """Returns {"value_proposition", "usps" (list), "updated_at"} - or a row
    of empty defaults if the account never saved a profile, so callers never
    have to special-case "no profile yet"."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM account_profiles WHERE account_id = ?", (account_id,)
        ).fetchone()
        if not row:
            return {"account_id": account_id, "value_proposition": "", "usps": [], "updated_at": None}
        usps = [line.strip() for line in (row["usps"] or "").split("\n") if line.strip()]
        return {"account_id": account_id, "value_proposition": row["value_proposition"], "usps": usps, "updated_at": row["updated_at"]}


def upsert_account_profile(account_id: int, value_proposition: str, usps: list) -> dict:
    usps_text = "\n".join(u.strip() for u in (usps or []) if u.strip())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO account_profiles (account_id, value_proposition, usps, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET
                value_proposition = EXCLUDED.value_proposition,
                usps = EXCLUDED.usps,
                updated_at = EXCLUDED.updated_at
            """,
            (account_id, (value_proposition or "").strip(), usps_text, now_iso()),
        )
    return get_account_profile(account_id)


def list_profile_questions(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM account_profile_questions WHERE account_id = ? ORDER BY id ASC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def replace_pending_profile_questions(account_id: int, questions: list) -> list:
    """Stores a fresh AI-verdiepingsronde: removes any question from a
    previous round that was never answered (a stale/skipped question), keeps
    already-answered ones as history, then inserts the new questions
    unanswered. Returns the full up-to-date list."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM account_profile_questions WHERE account_id = ? AND answer IS NULL", (account_id,)
        )
        created = now_iso()
        for q in questions:
            conn.execute(
                "INSERT INTO account_profile_questions (account_id, question, answer, created_at) VALUES (?, ?, NULL, ?)",
                (account_id, q, created),
            )
    return list_profile_questions(account_id)


def answer_profile_questions(account_id: int, answers: dict) -> list:
    """answers: {question_id: answer_text}. Silently ignores ids that don't
    belong to this account (defensive, same pattern as the rest of this
    file's tenant checks)."""
    with get_conn() as conn:
        for qid, answer in answers.items():
            conn.execute(
                "UPDATE account_profile_questions SET answer = ?, answered_at = ? WHERE id = ? AND account_id = ?",
                (answer, now_iso(), qid, account_id),
            )
    return list_profile_questions(account_id)


# ---------------------------------------------------------------------------
# Assignment (toewijzen aan een teamlid)
# ---------------------------------------------------------------------------

def assign_contact(contact_id: int, account_id: int, user_id):
    """user_id=None unassigns. Returns the updated contact, or None if the
    contact (or, when assigning, the user) doesn't belong to this account."""
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)).fetchone()
        if not owned:
            return None
        label = "niet-toegewezen"
        if user_id is not None:
            user = conn.execute("SELECT email FROM users WHERE id = ? AND account_id = ?", (user_id, account_id)).fetchone()
            if not user:
                return None
            label = user["email"]
        conn.execute("UPDATE contacts SET assigned_to = ? WHERE id = ?", (user_id, contact_id))
        log_contact_activity(account_id, contact_id, "assigned", f"Toegewezen aan {label}", _conn=conn)
        row = conn.execute(
            "SELECT c.*, u.email AS assigned_to_email FROM contacts c LEFT JOIN users u ON u.id = c.assigned_to WHERE c.id = ?",
            (contact_id,),
        ).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Exclusion (bestaande klanten / lopende offertes niet opnieuw benaderen)
# ---------------------------------------------------------------------------

def add_exclusion_entry(account_id: int, domain: str, company_name: str = "", reason: str = "", source: str = "manual") -> dict:
    domain = domain.strip().lower().lstrip("@")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO exclusion_entries (account_id, domain, company_name, reason, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id, domain) DO UPDATE SET
                company_name = excluded.company_name, reason = excluded.reason, source = excluded.source
            """,
            (account_id, domain, company_name, reason, source, now_iso()),
        )
        # Apply immediately to any contacts already on file for this domain.
        contacts = conn.execute(
            "SELECT id FROM contacts WHERE account_id = ? AND company_domain = ?", (account_id, domain)
        ).fetchall()
        for c in contacts:
            conn.execute(
                "UPDATE contacts SET excluded_reason = ? WHERE id = ? AND (excluded_reason IS NULL OR excluded_reason = '')",
                (f"{source}:{reason}" if reason else source, c["id"]),
            )
        row = conn.execute("SELECT * FROM exclusion_entries WHERE account_id = ? AND domain = ?", (account_id, domain)).fetchone()
        return dict(row)


def list_exclusion_entries(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM exclusion_entries WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_exclusion_entry(entry_id: int, account_id: int) -> bool:
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM exclusion_entries WHERE id = ? AND account_id = ?", (entry_id, account_id)).fetchone()
        if not owned:
            return False
        conn.execute("DELETE FROM exclusion_entries WHERE id = ?", (entry_id,))
        return True


def check_exclusion(account_id: int, domain: str, _conn=None):
    """Returns a reason string if this domain is on the account's exclusion
    list, else None. (The HubSpot live-check described in crm-roadmap.md
    Fase 2 will call in here too, once built.)"""
    if not domain:
        return None

    def _run(conn):
        row = conn.execute(
            "SELECT reason, source FROM exclusion_entries WHERE account_id = ? AND domain = ?", (account_id, domain)
        ).fetchone()
        if not row:
            return None
        return f"{row['source']}:{row['reason']}" if row["reason"] else row["source"]

    if _conn is not None:
        return _run(_conn)
    with get_conn() as conn:
        return _run(conn)


def set_contact_excluded_reason(contact_id: int, account_id: int, reason: str, _conn=None) -> bool:
    """Sets excluded_reason directly - for exclusion sources that live
    outside the manual is_customer/has_open_quote flags and the CSV
    exclusion list (see check_exclusion above): the live HubSpot check
    (crm-roadmap.md Fase 2, see hubspot_client.py). Only sets it if the
    contact isn't already excluded, so it never overwrites a more specific
    existing reason. Returns whether it actually changed anything."""
    def _run(conn):
        row = conn.execute(
            "SELECT excluded_reason FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)
        ).fetchone()
        if not row or row["excluded_reason"]:
            return False
        conn.execute("UPDATE contacts SET excluded_reason = ? WHERE id = ?", (reason, contact_id))
        log_contact_activity(account_id, contact_id, "excluded", f"Uitgesloten: {reason}", _conn=conn)
        return True

    if _conn is not None:
        return _run(_conn)
    with get_conn() as conn:
        return _run(conn)


def import_exclusion_csv_rows(account_id: int, rows: list) -> int:
    """rows: list of dicts with at least a 'domain' or 'email' key (a bare
    company name with no domain/email is skipped - nothing to match on).
    Returns the number of entries added/updated."""
    n = 0
    for r in rows:
        domain = (r.get("domain") or "").strip().lower()
        if not domain and r.get("email"):
            domain = _domain_from_email(r["email"])
        if not domain:
            continue
        add_exclusion_entry(account_id, domain, company_name=r.get("company", "") or r.get("company_name", ""),
                             reason=r.get("reason", ""), source="csv")
        n += 1
    return n


# ---------------------------------------------------------------------------
# Audit trail / timeline
# ---------------------------------------------------------------------------

def log_contact_activity(account_id: int, contact_id: int, event_type: str, description: str, meta: str = "", _conn=None) -> dict:
    def _run(conn):
        cur = conn.execute(
            "INSERT INTO contact_activity (account_id, contact_id, event_type, description, meta, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (account_id, contact_id, event_type, description, meta, now_iso()),
        )
        return {"id": cur.fetchone()["id"]}

    if _conn is not None:
        return _run(_conn)
    with get_conn() as conn:
        return _run(conn)


def contact_timeline(contact_id: int, account_id: int):
    """Everything that happened to one lead, newest first: CRM events
    (created/imported/tag/assignment/exclusion changes) plus every campaign
    email and opvolgsequence-mail sent/opened/clicked/form-filled to them,
    plus every LinkedIn outreach action logged against them. Returns None if
    the contact doesn't belong to this account.

    Fase 3: every "email_sent"/"email_failed" event also carries subject/body
    (the exact rendered text - see record_send_result()/record_sequence_send())
    when that was captured at send time; older sends (or ones from before
    this column existed) simply omit these keys, and the frontend falls back
    to showing "geen inhoud bewaard" for those."""
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)).fetchone()
        if not owned:
            return None

        events = []
        for a in conn.execute(
            "SELECT event_type, description, created_at FROM contact_activity WHERE contact_id = ? ORDER BY created_at",
            (contact_id,),
        ).fetchall():
            events.append({"type": a["event_type"], "description": a["description"], "at": a["created_at"]})

        for r in conn.execute(
            """
            SELECT camp.name AS campaign_name, cv.offer_name, cr.sent_at, cr.send_error, cr.opened_at,
                   cr.clicked_at, cr.form_filled_at, cr.rendered_subject, cr.rendered_body
            FROM campaign_recipients cr
            JOIN campaigns camp ON camp.id = cr.campaign_id
            JOIN campaign_variants cv ON cv.id = cr.variant_id
            WHERE cr.contact_id = ?
            """,
            (contact_id,),
        ).fetchall():
            label = f"{r['campaign_name']} ({r['offer_name']})"
            if r["sent_at"]:
                events.append({
                    "type": "email_sent", "description": f"E-mail verstuurd - {label}", "at": r["sent_at"],
                    "subject": r["rendered_subject"], "body": r["rendered_body"],
                })
            if r["send_error"]:
                events.append({
                    "type": "email_failed", "description": f"Verzenden mislukt - {label}: {r['send_error']}", "at": r["sent_at"] or "",
                    "subject": r["rendered_subject"], "body": r["rendered_body"],
                })
            if r["opened_at"]:
                events.append({"type": "email_opened", "description": f"E-mail geopend - {label}", "at": r["opened_at"]})
            if r["clicked_at"]:
                events.append({"type": "email_clicked", "description": f"Link geklikt - {label}", "at": r["clicked_at"]})
            if r["form_filled_at"]:
                events.append({"type": "email_form_filled", "description": f"Formulier ingevuld - {label}", "at": r["form_filled_at"]})

        for s in conn.execute(
            """
            SELECT seq.name AS sequence_name, ss.sent_at, ss.attempted_at, ss.send_error,
                   ss.rendered_subject, ss.rendered_body
            FROM sequence_sends ss
            JOIN sequence_enrollments se ON se.id = ss.enrollment_id
            JOIN sequences seq ON seq.id = se.sequence_id
            WHERE se.contact_id = ?
            """,
            (contact_id,),
        ).fetchall():
            label = f"opvolgsequence '{s['sequence_name']}'"
            if s["sent_at"]:
                events.append({
                    "type": "email_sent", "description": f"E-mail verstuurd - {label}", "at": s["sent_at"],
                    "subject": s["rendered_subject"], "body": s["rendered_body"],
                })
            elif s["send_error"]:
                events.append({
                    "type": "email_failed", "description": f"Verzenden mislukt - {label}: {s['send_error']}",
                    "at": s["attempted_at"] or "",
                    "subject": s["rendered_subject"], "body": s["rendered_body"],
                })

        for l in conn.execute(
            "SELECT action, template_label, note, created_at FROM linkedin_outreach WHERE contact_id = ?",
            (contact_id,),
        ).fetchall():
            desc = f"LinkedIn: {l['action']}"
            if l["template_label"]:
                desc += f" (template {l['template_label']})"
            if l["note"]:
                desc += f" - {l['note']}"
            events.append({"type": "linkedin_action", "description": desc, "at": l["created_at"]})

        events = [e for e in events if e["at"]]
        events.sort(key=lambda e: e["at"], reverse=True)
        return events


# ---------------------------------------------------------------------------
# Reminders (agenderen)
# ---------------------------------------------------------------------------

def create_reminder(account_id: int, contact_id: int, remind_at: str, note: str = "", created_by=None) -> dict:
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)).fetchone()
        if not owned:
            return None
        cur = conn.execute(
            "INSERT INTO reminders (account_id, contact_id, remind_at, note, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (account_id, contact_id, remind_at, note, created_by, now_iso()),
        )
        reminder_id = cur.fetchone()["id"]
        log_contact_activity(account_id, contact_id, "reminder_set", f"Herinnering gezet voor {remind_at}" + (f": {note}" if note else ""), _conn=conn)
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row)


def list_reminders(account_id: int, only_due: bool = False, only_open: bool = True) -> list:
    with get_conn() as conn:
        sql = """
            SELECT r.*, c.first_name, c.last_name, c.email, c.company
            FROM reminders r JOIN contacts c ON c.id = r.contact_id
            WHERE r.account_id = ?
        """
        params = [account_id]
        if only_open:
            sql += " AND r.status = 'open'"
        if only_due:
            sql += " AND r.remind_at <= ?"
            params.append(now_iso())
        sql += " ORDER BY r.remind_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def complete_reminder(reminder_id: int, account_id: int) -> bool:
    with get_conn() as conn:
        owned = conn.execute("SELECT 1 FROM reminders WHERE id = ? AND account_id = ?", (reminder_id, account_id)).fetchone()
        if not owned:
            return False
        conn.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,))
        return True


# ---------------------------------------------------------------------------
# Prospecting (Vibe Prospecting / Explorium) settings - per account,
# encrypted at rest, same pattern as smtp_settings. No live API calls here
# yet - see crm-roadmap.md, Fase 2.
# ---------------------------------------------------------------------------

def get_prospecting_settings(account_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM prospecting_settings WHERE account_id = ?", (account_id,)).fetchone()
        return dict(row) if row else None


def save_prospecting_settings(account_id: int, api_key_encrypted: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO prospecting_settings (account_id, api_key_encrypted, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET api_key_encrypted = excluded.api_key_encrypted, updated_at = excluded.updated_at
            """,
            (account_id, api_key_encrypted, now_iso()),
        )
        row = conn.execute("SELECT * FROM prospecting_settings WHERE account_id = ?", (account_id,)).fetchone()
        return dict(row)


def delete_prospecting_settings(account_id: int) -> bool:
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM prospecting_settings WHERE account_id = ?", (account_id,)).fetchone()
        if not existing:
            return False
        conn.execute("DELETE FROM prospecting_settings WHERE account_id = ?", (account_id,))
        return True


# ---------------------------------------------------------------------------
# HubSpot settings - per account, encrypted at rest. No live API calls yet -
# see crm-roadmap.md, Fase 2.
# ---------------------------------------------------------------------------

def get_hubspot_settings(account_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM hubspot_settings WHERE account_id = ?", (account_id,)).fetchone()
        return dict(row) if row else None


def save_hubspot_settings(account_id: int, access_token_encrypted: str, exclude_customers: bool = True, exclude_open_deals: bool = True) -> dict:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO hubspot_settings (account_id, access_token_encrypted, exclude_customers, exclude_open_deals, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET
                access_token_encrypted = excluded.access_token_encrypted,
                exclude_customers = excluded.exclude_customers,
                exclude_open_deals = excluded.exclude_open_deals,
                updated_at = excluded.updated_at
            """,
            (account_id, access_token_encrypted, 1 if exclude_customers else 0, 1 if exclude_open_deals else 0, now_iso()),
        )
        row = conn.execute("SELECT * FROM hubspot_settings WHERE account_id = ?", (account_id,)).fetchone()
        return dict(row)


def delete_hubspot_settings(account_id: int) -> bool:
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM hubspot_settings WHERE account_id = ?", (account_id,)).fetchone()
        if not existing:
            return False
        conn.execute("DELETE FROM hubspot_settings WHERE account_id = ?", (account_id,))
        return True


# ---------------------------------------------------------------------------
# Fase 2: bezwaren-bibliotheek (objection_templates) - seeded per account
# from DEFAULT_OBJECTIONS at create_account() time, editable afterwards.
# ---------------------------------------------------------------------------

def list_objection_templates(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM objection_templates WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def create_objection_template(account_id: int, category: str, keywords: str, suggested_reply: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO objection_templates (account_id, category, keywords, suggested_reply) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (account_id, category, keywords, suggested_reply),
        )
        template_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM objection_templates WHERE id = ?", (template_id,)).fetchone()
        return dict(row)


def update_objection_template(template_id: int, account_id: int, category: str = None,
                               keywords: str = None, suggested_reply: str = None):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM objection_templates WHERE id = ? AND account_id = ?", (template_id, account_id)
        ).fetchone()
        if not existing:
            return None
        conn.execute(
            "UPDATE objection_templates SET category = ?, keywords = ?, suggested_reply = ? "
            "WHERE id = ? AND account_id = ?",
            (
                category if category is not None else existing["category"],
                keywords if keywords is not None else existing["keywords"],
                suggested_reply if suggested_reply is not None else existing["suggested_reply"],
                template_id, account_id,
            ),
        )
        row = conn.execute("SELECT * FROM objection_templates WHERE id = ?", (template_id,)).fetchone()
        return dict(row)


def delete_objection_template(template_id: int, account_id: int) -> bool:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM objection_templates WHERE id = ? AND account_id = ?", (template_id, account_id)
        ).fetchone()
        if not existing:
            return False
        conn.execute("DELETE FROM objection_templates WHERE id = ? AND account_id = ?", (template_id, account_id))
        return True


# ---------------------------------------------------------------------------
# Email Generator: opgeslagen AI-gegenereerde cold-outreach e-mails
# (email_generator_templates).
# ---------------------------------------------------------------------------

def list_email_generator_templates(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM email_generator_templates WHERE account_id = ? ORDER BY id DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def create_email_generator_template(account_id: int, sector: str, persona: str, goal: str, stage: str,
                                     tone: str, subject: str, body: str, angle: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO email_generator_templates "
            "(account_id, sector, persona, goal, stage, tone, subject, body, angle, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (account_id, sector, persona, goal, stage, tone, subject, body, angle, now_iso()),
        )
        template_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM email_generator_templates WHERE id = ?", (template_id,)).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Fase 2: incoming replies (via IMAP - see imap_client.py) + AI/template
# concept-antwoorden (reply_drafts - see ai_client.py) + de
# "automatisch versturen"-instelling (accounts.auto_reply_enabled).
# ---------------------------------------------------------------------------

def get_imap_last_uid(account_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT imap_last_uid FROM smtp_settings WHERE account_id = ?", (account_id,)).fetchone()
        return row["imap_last_uid"] if row else 0


def update_imap_last_uid(account_id: int, uid: int):
    with get_conn() as conn:
        conn.execute("UPDATE smtp_settings SET imap_last_uid = ? WHERE account_id = ?", (uid, account_id))


def record_incoming_reply(account_id: int, from_email: str, subject: str, body: str,
                           message_uid: str, received_at: str, objection_category: str = "") -> dict | None:
    """Stores one fetched IMAP message, matched (if possible) to a contact
    and that contact's most recently sent campaign. Returns None if this
    message_uid was already processed for this account (keeps polling
    idempotent - see the UNIQUE(account_id, message_uid) constraint).

    Also stops any active sequence enrollment for the matched contact (Fase
    2 "Aanvullingen": a sequence stops automatically once the contact
    replies, rather than sending another follow-up after an answer)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM incoming_replies WHERE account_id = ? AND message_uid = ?",
            (account_id, message_uid),
        ).fetchone()
        if existing:
            return None

        contact = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? AND lower(email) = lower(?)",
            (account_id, from_email),
        ).fetchone()
        contact_id = contact["id"] if contact else None

        campaign_id = None
        if contact_id:
            rec = conn.execute(
                """
                SELECT cr.campaign_id FROM campaign_recipients cr
                JOIN campaigns c ON c.id = cr.campaign_id
                WHERE cr.contact_id = ? AND c.account_id = ?
                ORDER BY cr.sent_at DESC NULLS LAST, cr.id DESC
                LIMIT 1
                """,
                (contact_id, account_id),
            ).fetchone()
            campaign_id = rec["campaign_id"] if rec else None

        cur = conn.execute(
            """
            INSERT INTO incoming_replies
                (account_id, contact_id, campaign_id, from_email, subject, body,
                 message_uid, objection_category, received_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (account_id, contact_id, campaign_id, from_email, subject, body,
             message_uid, objection_category, received_at, now_iso()),
        )
        reply_id = cur.fetchone()["id"]

        if contact_id:
            log_contact_activity(
                account_id, contact_id, "reply_received",
                f"Reply ontvangen: {subject}" if subject else "Reply ontvangen",
                _conn=conn,
            )
            conn.execute(
                """
                UPDATE sequence_enrollments SET status = 'stopped_reply', stopped_reason = ?
                WHERE account_id = ? AND contact_id = ? AND status = 'active'
                """,
                (f"Contact reageerde op {received_at}", account_id, contact_id),
            )

        row = conn.execute("SELECT * FROM incoming_replies WHERE id = ?", (reply_id,)).fetchone()
        return dict(row)


def list_incoming_replies(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ir.*, c.first_name, c.last_name, c.company,
                   rd.id AS draft_id, rd.status AS draft_status, rd.draft_body, rd.source AS draft_source
            FROM incoming_replies ir
            LEFT JOIN contacts c ON c.id = ir.contact_id
            LEFT JOIN reply_drafts rd ON rd.incoming_reply_id = ir.id
            WHERE ir.account_id = ?
            ORDER BY ir.received_at DESC
            """,
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_incoming_reply(reply_id: int, account_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT ir.*, c.first_name, c.last_name, c.company
            FROM incoming_replies ir LEFT JOIN contacts c ON c.id = ir.contact_id
            WHERE ir.id = ? AND ir.account_id = ?
            """,
            (reply_id, account_id),
        ).fetchone()
        return dict(row) if row else None


def create_reply_draft(account_id: int, incoming_reply_id: int, draft_body: str, source: str = "template") -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reply_drafts (account_id, incoming_reply_id, draft_body, source, created_at) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (account_id, incoming_reply_id, draft_body, source, now_iso()),
        )
        draft_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row)


def list_reply_drafts(account_id: int, status: str = None) -> list:
    with get_conn() as conn:
        query = """
            SELECT rd.*, ir.from_email, ir.subject, ir.body AS reply_body, ir.objection_category,
                   ir.contact_id, c.first_name, c.last_name, c.company
            FROM reply_drafts rd
            JOIN incoming_replies ir ON ir.id = rd.incoming_reply_id
            LEFT JOIN contacts c ON c.id = ir.contact_id
            WHERE rd.account_id = ?
        """
        params = [account_id]
        if status:
            query += " AND rd.status = ?"
            params.append(status)
        query += " ORDER BY rd.created_at DESC"
        rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def get_reply_draft(draft_id: int, account_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT rd.*, ir.from_email, ir.subject, ir.contact_id, ir.campaign_id
            FROM reply_drafts rd JOIN incoming_replies ir ON ir.id = rd.incoming_reply_id
            WHERE rd.id = ? AND rd.account_id = ?
            """,
            (draft_id, account_id),
        ).fetchone()
        return dict(row) if row else None


def update_reply_draft(draft_id: int, account_id: int, status: str, draft_body: str = None,
                        reviewed_by=None, sent_error: str = None) -> bool:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM reply_drafts WHERE id = ? AND account_id = ?", (draft_id, account_id)
        ).fetchone()
        if not existing:
            return False
        conn.execute(
            """
            UPDATE reply_drafts SET
                status = ?,
                draft_body = COALESCE(?, draft_body),
                reviewed_at = ?,
                reviewed_by = COALESCE(?, reviewed_by),
                sent_error = ?
            WHERE id = ? AND account_id = ?
            """,
            (status, draft_body, now_iso(), reviewed_by, sent_error, draft_id, account_id),
        )
        return True


def get_auto_reply_enabled(account_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT auto_reply_enabled FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return bool(row["auto_reply_enabled"]) if row else False


def set_auto_reply_enabled(account_id: int, enabled: bool):
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET auto_reply_enabled = ? WHERE id = ?", (1 if enabled else 0, account_id))


# ---------------------------------------------------------------------------
# Fase 2: opvolgmail-sequenties (drip campaigns). Elke sequence_step wacht
# `wait_days` NA die stap voordat de volgende verstuurd wordt - stap 0 gaat
# direct uit zodra een contact wordt ingeschreven. Een enrollment stopt
# automatisch bij een reply (record_incoming_reply hierboven) of wanneer het
# contact op het moment van versturen do_not_contact/excluded blijkt
# (skip_enrollment - zie app.py's verwerkings-endpoint).
# ---------------------------------------------------------------------------

def create_sequence(account_id: int, name: str, steps: list, persona_id: int = None) -> dict:
    """steps: [{"wait_days": int, "subject_template": str, "body_template": str}, ...] in order.
    persona_id (optional): ties this sequence to one buyer persona (Fase 3) -
    see auto_enroll_by_persona() below for how that's used."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sequences (account_id, name, persona_id, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (account_id, name, persona_id, now_iso()),
        )
        sequence_id = cur.fetchone()["id"]
        for i, step in enumerate(steps):
            conn.execute(
                "INSERT INTO sequence_steps (sequence_id, step_order, wait_days, subject_template, body_template) "
                "VALUES (?, ?, ?, ?, ?)",
                (sequence_id, i, step["wait_days"], step["subject_template"], step["body_template"]),
            )
    return get_sequence(sequence_id, account_id)


def get_sequence(sequence_id: int, account_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.*, bp.name AS persona_name
            FROM sequences s LEFT JOIN buyer_personas bp ON bp.id = s.persona_id
            WHERE s.id = ? AND s.account_id = ?
            """,
            (sequence_id, account_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        steps = conn.execute(
            "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY step_order", (sequence_id,)
        ).fetchall()
        result["steps"] = [dict(s) for s in steps]
        return result


def list_sequences(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.*, bp.name AS persona_name
            FROM sequences s LEFT JOIN buyer_personas bp ON bp.id = s.persona_id
            WHERE s.account_id = ? ORDER BY s.created_at DESC
            """,
            (account_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            steps = conn.execute(
                "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY step_order", (row["id"],)
            ).fetchall()
            d["steps"] = [dict(s) for s in steps]
            counts = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'active') AS active,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status = 'stopped_reply') AS stopped_reply
                FROM sequence_enrollments WHERE sequence_id = ?
                """,
                (row["id"],),
            ).fetchone()
            d["enrollment_counts"] = dict(counts)
            result.append(d)
        return result


def set_sequence_status(sequence_id: int, account_id: int, status: str) -> bool:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM sequences WHERE id = ? AND account_id = ?", (sequence_id, account_id)
        ).fetchone()
        if not existing:
            return False
        conn.execute("UPDATE sequences SET status = ? WHERE id = ?", (status, sequence_id))
        return True


def enroll_contact(sequence_id: int, account_id: int, contact_id: int):
    """Enrolls at step 0, due immediately (the next processing tick sends
    it). Returns None if the sequence/contact doesn't exist for this
    account; returns the existing enrollment unchanged if already enrolled
    (idempotent - safe to call again e.g. from a bulk-enroll action)."""
    with get_conn() as conn:
        seq = conn.execute(
            "SELECT 1 FROM sequences WHERE id = ? AND account_id = ?", (sequence_id, account_id)
        ).fetchone()
        contact = conn.execute(
            "SELECT 1 FROM contacts WHERE id = ? AND account_id = ?", (contact_id, account_id)
        ).fetchone()
        if not seq or not contact:
            return None
        existing = conn.execute(
            "SELECT * FROM sequence_enrollments WHERE sequence_id = ? AND contact_id = ?",
            (sequence_id, contact_id),
        ).fetchone()
        if existing:
            return dict(existing)
        cur = conn.execute(
            """
            INSERT INTO sequence_enrollments
                (sequence_id, account_id, contact_id, current_step, status, next_send_at, enrolled_at)
            VALUES (?, ?, ?, 0, 'active', ?, ?)
            RETURNING id
            """,
            (sequence_id, account_id, contact_id, now_iso(), now_iso()),
        )
        enrollment_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM sequence_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        return dict(row)


def auto_enroll_by_persona(account_id: int) -> dict:
    """Fase 3: "automatisch inschrijven o.b.v. persona" - voor elk contact met
    een buyer persona dat nog nergens actief is ingeschreven, zoekt de actieve
    sequence die aan diezelfde persona gekoppeld is (sequences.persona_id) en
    schrijft het contact daarin in. Contacten zonder persona, of met een
    persona zonder bijpassende actieve sequence, worden overgeslagen (geen
    generieke fallback-sequence - dat blijft een bewuste, aparte keuze via de
    bestaande handmatige inschrijf-flow). Idempotent: opnieuw draaien
    schrijft niemand dubbel in."""
    with get_conn() as conn:
        candidates = conn.execute(
            """
            SELECT c.id AS contact_id, c.persona_id
            FROM contacts c
            WHERE c.account_id = ? AND c.persona_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM sequence_enrollments se
                  WHERE se.contact_id = c.id AND se.status = 'active'
              )
            """,
            (account_id,),
        ).fetchall()
        enrolled, skipped_no_sequence = 0, 0
        for row in candidates:
            seq = conn.execute(
                "SELECT id FROM sequences WHERE account_id = ? AND persona_id = ? AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1",
                (account_id, row["persona_id"]),
            ).fetchone()
            if not seq:
                skipped_no_sequence += 1
                continue
            existing = conn.execute(
                "SELECT 1 FROM sequence_enrollments WHERE sequence_id = ? AND contact_id = ?",
                (seq["id"], row["contact_id"]),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO sequence_enrollments
                    (sequence_id, account_id, contact_id, current_step, status, next_send_at, enrolled_at)
                VALUES (?, ?, ?, 0, 'active', ?, ?)
                """,
                (seq["id"], account_id, row["contact_id"], now_iso(), now_iso()),
            )
            enrolled += 1
        return {"enrolled": enrolled, "skipped_no_matching_sequence": skipped_no_sequence}


def list_enrollments(sequence_id: int, account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT se.*, c.first_name, c.last_name, c.email, c.company
            FROM sequence_enrollments se JOIN contacts c ON c.id = se.contact_id
            WHERE se.sequence_id = ? AND se.account_id = ?
            ORDER BY se.enrolled_at DESC
            """,
            (sequence_id, account_id),
        ).fetchall()
        return [dict(r) for r in rows]


def due_enrollments(now: str = None) -> list:
    """Across ALL accounts - used by the sequence-processing endpoint. Only
    enrollments whose sequence is still active and whose next_send_at has
    passed; contact-level do_not_contact/exclusion is checked at send-time
    by the caller (app.py), not here, since that can change after
    enrollment."""
    now = now or now_iso()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT se.*, s.account_id AS seq_account_id,
                   c.first_name, c.last_name, c.email, c.company,
                   c.do_not_contact, c.excluded_reason
            FROM sequence_enrollments se
            JOIN sequences s ON s.id = se.sequence_id
            JOIN contacts c ON c.id = se.contact_id
            WHERE se.status = 'active' AND s.status = 'active' AND se.next_send_at <= ?
            """,
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def record_sequence_send(enrollment_id: int, step_id: int, sent: bool, error: str = None,
                          rendered_subject: str = None, rendered_body: str = None):
    """Logs the send attempt and advances the enrollment to the next step
    (schedules it wait_days from now), or marks the enrollment 'completed'
    if that was the last step. rendered_subject/rendered_body (Fase 3): the
    exact, personalized text that was (attempted to be) sent - stored so the
    contact's audit trail can show precisely what they received, not just
    the template."""
    with get_conn() as conn:
        now = now_iso()
        conn.execute(
            """
            INSERT INTO sequence_sends (enrollment_id, step_id, sent_at, send_error, rendered_subject, rendered_body, attempted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (enrollment_id, step_id, now if sent else None, error, rendered_subject, rendered_body, now),
        )
        enrollment = conn.execute("SELECT * FROM sequence_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        next_step_order = enrollment["current_step"] + 1
        next_step = conn.execute(
            "SELECT * FROM sequence_steps WHERE sequence_id = ? AND step_order = ?",
            (enrollment["sequence_id"], next_step_order),
        ).fetchone()
        if next_step:
            next_send_at = (datetime.now(timezone.utc) + timedelta(days=next_step["wait_days"])).isoformat()
            conn.execute(
                "UPDATE sequence_enrollments SET current_step = ?, next_send_at = ? WHERE id = ?",
                (next_step_order, next_send_at, enrollment_id),
            )
        else:
            conn.execute("UPDATE sequence_enrollments SET status = 'completed' WHERE id = ?", (enrollment_id,))


def skip_enrollment(enrollment_id: int, reason: str):
    """A due enrollment whose contact turned out to be do_not_contact or
    excluded at send-time: stop it rather than retrying forever."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE sequence_enrollments SET status = 'stopped_excluded', stopped_reason = ? WHERE id = ?",
            (reason, enrollment_id),
        )


# ---------------------------------------------------------------------------
# Support: FAQ / knowledge base + support tickets
# ---------------------------------------------------------------------------

DEFAULT_KB_ARTICLES = [
    ("Aan de slag", "Hoe voeg ik contacten toe?",
     "Ga naar het tabblad Contacten en klik op 'Contact toevoegen', of importeer een heel bestand via "
     "'Importeren' (CSV of Vibe Prospecting) in het tabblad Integraties."),
    ("Aan de slag", "Hoe verstuur ik mijn eerste campagne?",
     "Voeg eerst contacten toe, ga dan naar het tabblad Campagnes, maak een nieuwe campagne aan en klik op "
     "'Versturen'. Je kunt de resultaten (geopend/geklikt) volgen op het campagne-overzicht."),
    ("Mail", "Kan ik mailen vanuit mijn eigen domein?",
     "Ja - ga naar Integraties > Mail-instellingen en vul de SMTP-gegevens van je eigen mailaccount in. "
     "Zonder eigen instellingen wordt het gedeelde afzenderadres van het platform gebruikt."),
    ("Mail", "Waarom komen replies niet binnen?",
     "Reply-tracking heeft naast verzendgegevens (SMTP) ook IMAP-gegevens nodig, zodat het platform je "
     "inbox mag uitlezen. Vul beide in bij Integraties > Mail-instellingen."),
    ("CRM", "Hoe voorkom ik dat bestaande klanten worden benaderd?",
     "Gebruik de uitsluitlijst in Integraties: markeer een contact handmatig als 'klant' of 'offerte loopt', "
     "upload een CSV met te vermijden bedrijven, of koppel HubSpot voor een live-check."),
    ("CRM", "Wat is het verschil tussen 'uitgesloten' en 'niet meer benaderen'?",
     "'Uitgesloten' komt van de uitsluitlijst (klant/offerte) en kan automatisch worden opgeheven als die "
     "status wijzigt. 'Niet meer benaderen' is een expliciete, blijvende stop die je zelf per contact zet."),
]


def _seed_default_kb_articles(conn):
    existing = conn.execute("SELECT COUNT(*) AS n FROM kb_articles").fetchone()
    if existing["n"] > 0:
        return
    for i, (category, question, answer) in enumerate(DEFAULT_KB_ARTICLES):
        conn.execute(
            "INSERT INTO kb_articles (category, question, answer, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
            (category, question, answer, i, now_iso()),
        )


def list_kb_articles(q: str = None) -> list:
    with get_conn() as conn:
        if q:
            like = f"%{q.lower()}%"
            rows = conn.execute(
                "SELECT * FROM kb_articles WHERE LOWER(question) LIKE ? OR LOWER(answer) LIKE ? OR LOWER(category) LIKE ? ORDER BY sort_order",
                (like, like, like),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM kb_articles ORDER BY sort_order").fetchall()
        return [dict(r) for r in rows]


def create_support_ticket(account_id: int, user_id, subject: str, message: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO support_tickets (account_id, user_id, subject, message, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (account_id, user_id, subject, message, now_iso()),
        )
        ticket_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row)


def list_support_tickets(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM support_tickets WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_support_tickets() -> list:
    """For the superadmin/support dashboard - across every account."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT st.*, a.company_name
            FROM support_tickets st JOIN accounts a ON a.id = st.account_id
            ORDER BY (st.status = 'open') DESC, st.created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def reply_support_ticket(ticket_id: int, admin_reply: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE support_tickets SET admin_reply = ?, status = 'replied', replied_at = ? WHERE id = ?",
            (admin_reply, now_iso(), ticket_id),
        )
        row = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Campaigns / A-B test (scoped per account)
# ---------------------------------------------------------------------------

def create_campaign(account_id: int, name: str, variants: list, include_excluded: bool = False,
                     only_persona_id: int = None) -> dict:
    """
    variants: list of dicts with keys group_label, offer_name, subject_template,
    body_template, and an optional persona_id (Fase 3 - ties a variant to one
    buyer persona instead of the generic A/B split).
    Assigns every current, non-excluded contact of this account across the
    given variants and creates one campaign_recipients row (with its own
    tracking token) per contact. A contact whose buyer persona matches a
    variant's persona_id always gets that variant (round-robin among just
    that persona's variants, if it has more than one - so A/B testing still
    works within a persona); everyone else round-robins across the
    persona-less ("generic") variants as before. If a campaign has ONLY
    persona-tagged variants, a contact with no matching persona still falls
    back to a plain round-robin across all variants, so nobody is silently
    skipped. Nothing is sent yet - see launch_campaign(). Contacts marked
    "niet meer benaderen" (do_not_contact) or matched by the uitsluitlijst
    (excluded_reason - existing customer/open quote) are skipped by default,
    so a prospecting campaign never re-approaches them and never frustrates a
    live offerte-traject - pass include_excluded=True to deliberately
    override this (e.g. a non-sales announcement that should reach
    everyone). only_persona_id (Fase 3): restricts the whole campaign to
    contacts with that one buyer persona - the simple, UI-driven way to send
    a persona-targeted round of the existing (e.g. default) variants,
    independent of any per-variant persona_id above.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (account_id, name, status, created_at) VALUES (?, ?, 'draft', ?) RETURNING id",
            (account_id, name, now_iso()),
        )
        campaign_id = cur.fetchone()["id"]

        variant_ids = []
        persona_variant_ids = defaultdict(list)
        generic_variant_ids = []
        for v in variants:
            persona_id = v.get("persona_id")
            vcur = conn.execute(
                """
                INSERT INTO campaign_variants (campaign_id, group_label, offer_name, subject_template, body_template, persona_id)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (campaign_id, v["group_label"], v["offer_name"], v["subject_template"], v["body_template"], persona_id),
            )
            variant_id = vcur.fetchone()["id"]
            variant_ids.append(variant_id)
            if persona_id:
                persona_variant_ids[persona_id].append(variant_id)
            else:
                generic_variant_ids.append(variant_id)

        contacts_sql = "SELECT id, persona_id FROM contacts WHERE account_id = ? AND do_not_contact = 0"
        contacts_params = [account_id]
        if not include_excluded:
            contacts_sql += " AND (excluded_reason IS NULL OR excluded_reason = '')"
        if only_persona_id is not None:
            contacts_sql += " AND persona_id = ?"
            contacts_params.append(only_persona_id)
        contacts_sql += " ORDER BY id"
        contacts = conn.execute(contacts_sql, contacts_params).fetchall()

        persona_counters = defaultdict(int)
        generic_counter = 0
        for contact in contacts:
            persona_id = contact["persona_id"]
            if persona_id and persona_variant_ids.get(persona_id):
                pool = persona_variant_ids[persona_id]
                variant_id = pool[persona_counters[persona_id] % len(pool)]
                persona_counters[persona_id] += 1
            elif generic_variant_ids:
                variant_id = generic_variant_ids[generic_counter % len(generic_variant_ids)]
                generic_counter += 1
            elif variant_ids:
                variant_id = variant_ids[generic_counter % len(variant_ids)]
                generic_counter += 1
            else:
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
            """
            SELECT cv.*, bp.name AS persona_name
            FROM campaign_variants cv LEFT JOIN buyer_personas bp ON bp.id = cv.persona_id
            WHERE cv.campaign_id = ?
            """,
            (campaign_id,),
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


def record_send_result(recipient_id: int, sent: bool, error: str = None,
                        rendered_subject: str = None, rendered_body: str = None):
    """rendered_subject/rendered_body (Fase 3): the exact, personalized email
    text that was (attempted to be) sent to this recipient, stored regardless
    of success/failure so the contact's audit trail can show precisely what
    they received - not just a reference to the (possibly since-edited)
    template."""
    with get_conn() as conn:
        if sent:
            conn.execute(
                """
                UPDATE campaign_recipients
                SET sent_at = ?, send_error = NULL, rendered_subject = ?, rendered_body = ?
                WHERE id = ?
                """,
                (now_iso(), rendered_subject, rendered_body, recipient_id),
            )
        else:
            conn.execute(
                """
                UPDATE campaign_recipients
                SET send_error = ?, rendered_subject = ?, rendered_body = ?
                WHERE id = ?
                """,
                (error, rendered_subject, rendered_body, recipient_id),
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
# ICP-scoring (Fase 3, crm-roadmap.md: "analyse welke combinaties (sector x
# persona x omzet) het beste presteren"). Scoort elke waarde van sector,
# buyer persona en omzetcategorie (los, en als combinatie) op basis van de
# al bestaande open/click/reply-data uit campagnes en replies - geen nieuwe
# tracking nodig, dit hergebruikt gewoon wat er al gemeten wordt.
#
# Waarom reply het zwaarst weegt: een open kan per ongeluk zijn (afbeeldingen
# automatisch geladen), een klik is interesse, maar een reply is het enige
# signaal dat de ontvanger daadwerkelijk heeft gereageerd - de sterkste
# indicatie van een goede fit. Vandaar 50/30/20 (reply/click/open).
#
# MIN_SAMPLE voorkomt dat één toevallige open op 1 verzonden mail als
# "100% score" bovenaan komt te staan - zulke groepen worden wel getoond
# (voor transparantie) maar krijgen sufficient_data=False en tellen niet
# mee voor de aanbevolen ICP.
# ---------------------------------------------------------------------------

ICP_MIN_SAMPLE = 3
_ICP_UNKNOWN = "Onbekend"


def _icp_contact_stats(account_id: int, conn) -> dict:
    """Per-contact {sent, opened, clicked, replied} - campagne-tracking plus
    opvolgsequenties (die laatste hebben geen open/click-tracking, alleen
    verzonden, zie sequence_sends.SCHEMA-commentaar elders in dit bestand)."""
    stats = {}

    for row in conn.execute(
        """
        SELECT cr.contact_id,
               SUM(CASE WHEN cr.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN cr.opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened,
               SUM(CASE WHEN cr.clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked
        FROM campaign_recipients cr
        JOIN campaigns camp ON camp.id = cr.campaign_id
        WHERE camp.account_id = ?
        GROUP BY cr.contact_id
        """,
        (account_id,),
    ).fetchall():
        s = stats.setdefault(row["contact_id"], {"sent": 0, "opened": 0, "clicked": 0, "replied": 0})
        s["sent"] += row["sent"] or 0
        s["opened"] += row["opened"] or 0
        s["clicked"] += row["clicked"] or 0

    for row in conn.execute(
        """
        SELECT se.contact_id, SUM(CASE WHEN ss.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent
        FROM sequence_sends ss
        JOIN sequence_enrollments se ON se.id = ss.enrollment_id
        WHERE se.account_id = ?
        GROUP BY se.contact_id
        """,
        (account_id,),
    ).fetchall():
        s = stats.setdefault(row["contact_id"], {"sent": 0, "opened": 0, "clicked": 0, "replied": 0})
        s["sent"] += row["sent"] or 0

    for row in conn.execute(
        "SELECT DISTINCT contact_id FROM incoming_replies WHERE account_id = ? AND contact_id IS NOT NULL",
        (account_id,),
    ).fetchall():
        s = stats.setdefault(row["contact_id"], {"sent": 0, "opened": 0, "clicked": 0, "replied": 0})
        s["replied"] = 1

    return stats


def _icp_score_group(agg: dict) -> dict:
    sent = agg["emails_sent"]
    reached = agg["contacts_reached"]
    open_rate = agg["opens"] / sent if sent else 0.0
    click_rate = agg["clicks"] / sent if sent else 0.0
    reply_rate = agg["replied_contacts"] / reached if reached else 0.0
    score = round(100 * (0.5 * reply_rate + 0.3 * click_rate + 0.2 * open_rate), 1)
    return {
        **agg,
        "open_rate": round(open_rate, 3),
        "click_rate": round(click_rate, 3),
        "reply_rate": round(reply_rate, 3),
        "score": score,
        "sufficient_data": sent >= ICP_MIN_SAMPLE,
    }


def icp_scores(account_id: int) -> dict:
    """Scoort sector/persona/omzetcategorie (los en gecombineerd) op
    open/click/reply-performance. Zie de module-commentaar hierboven voor de
    scoreformule en de MIN_SAMPLE-afkap."""
    with get_conn() as conn:
        contacts = conn.execute(
            """
            SELECT c.id, c.sector, c.revenue_range, bp.name AS persona_name
            FROM contacts c LEFT JOIN buyer_personas bp ON bp.id = c.persona_id
            WHERE c.account_id = ?
            """,
            (account_id,),
        ).fetchall()
        contact_stats = _icp_contact_stats(account_id, conn)

    def bucket(value):
        return value.strip() if value and value.strip() else None

    dims = {"sector": {}, "persona": {}, "revenue_range": {}}
    combos = {}
    missing = {"sector": 0, "persona": 0, "revenue_range": 0}
    contacts_total = len(contacts)

    def add(groups: dict, key, contact_id):
        g = groups.setdefault(key, {"contacts": 0, "contacts_reached": 0, "emails_sent": 0, "opens": 0, "clicks": 0, "replied_contacts": 0})
        g["contacts"] += 1
        s = contact_stats.get(contact_id)
        if s and s["sent"] > 0:
            g["contacts_reached"] += 1
            g["emails_sent"] += s["sent"]
            g["opens"] += s["opened"]
            g["clicks"] += s["clicked"]
            g["replied_contacts"] += s["replied"]

    for c in contacts:
        sector = bucket(c["sector"])
        persona = bucket(c["persona_name"])
        revenue = bucket(c["revenue_range"])
        if not sector:
            missing["sector"] += 1
        if not persona:
            missing["persona"] += 1
        if not revenue:
            missing["revenue_range"] += 1

        if sector:
            add(dims["sector"], sector, c["id"])
        if persona:
            add(dims["persona"], persona, c["id"])
        if revenue:
            add(dims["revenue_range"], revenue, c["id"])
        if sector and persona and revenue:
            add(combos, (sector, persona, revenue), c["id"])

    def scored_list(groups: dict, label_key="value"):
        out = [{**_icp_score_group(agg), label_key: key} for key, agg in groups.items()]
        out.sort(key=lambda r: (r["sufficient_data"], r["score"]), reverse=True)
        return out

    dimensions = {name: scored_list(groups) for name, groups in dims.items()}

    combo_list = []
    for (sector, persona, revenue), agg in combos.items():
        combo_list.append({**_icp_score_group(agg), "sector": sector, "persona": persona, "revenue_range": revenue})
    combo_list.sort(key=lambda r: (r["sufficient_data"], r["score"]), reverse=True)

    recommended = None
    sufficient_combos = [c for c in combo_list if c["sufficient_data"]]
    if sufficient_combos:
        recommended = {"basis": "combinatie", **sufficient_combos[0]}
    else:
        best_per_dim = {}
        for name in ("sector", "persona", "revenue_range"):
            candidates = [r for r in dimensions[name] if r["sufficient_data"]]
            if candidates:
                best_per_dim[name] = candidates[0]
        if best_per_dim:
            recommended = {
                "basis": "losse_dimensies",
                "note": (
                    "Nog geen enkele sector x persona x omzet-combinatie met genoeg data "
                    f"(minimaal {ICP_MIN_SAMPLE} verzonden mails) - dit zijn de sterkste "
                    "losse signalen tot nu toe."
                ),
                **{name: r for name, r in best_per_dim.items()},
            }

    return {
        "dimensions": dimensions,
        "combinations": combo_list,
        "recommended_icp": recommended,
        "data_quality": {
            "contacts_total": contacts_total,
            "contacts_missing_sector": missing["sector"],
            "contacts_missing_persona": missing["persona"],
            "contacts_missing_revenue_range": missing["revenue_range"],
            "min_sample_size": ICP_MIN_SAMPLE,
        },
    }


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


# ---------------------------------------------------------------------------
# Superadmin / support
#
# Separate from the per-account "users" above: an admin login is Twikey
# staff, not tied to any one customer account, and can see an overview of
# every account plus a read-mostly detail view (users/contacts/campaigns/
# LinkedIn stats) for support purposes. Deliberately NOT the same thing as
# "logging in as" a customer (no impersonation) - see api_superadmin_* in
# app.py.
# ---------------------------------------------------------------------------

ADMIN_SESSION_LIFETIME_DAYS = 14  # shorter than a customer session (30 days) - elevated privileges


def create_admin(email: str, password: str) -> dict:
    """Create a support/superadmin login. Bootstrapped the same way as the
    very first customer account - via the ADMIN_SECRET-gated endpoint, see
    api_superadmin_create_admin in app.py - since there's no other admin yet
    to invite one."""
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO admins (email, password_hash, created_at) VALUES (?, ?, ?) RETURNING id",
            (email.lower(), password_hash, now_iso()),
        )
        admin_id = cur.fetchone()["id"]
        row = conn.execute("SELECT id, email, created_at FROM admins WHERE id = ?", (admin_id,)).fetchone()
        return dict(row)


def get_admin_by_email(email: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, created_at FROM admins WHERE email = ?", (email.lower(),)
        ).fetchone()
        return dict(row) if row else None


def verify_admin_password(email: str, password: str):
    """Return the admin identity dict (without password_hash) if credentials
    are correct, else None."""
    admin = get_admin_by_email(email)
    if not admin:
        return None
    if not bcrypt.checkpw(password.encode("utf-8"), admin["password_hash"].encode("utf-8")):
        return None
    return {"id": admin["id"], "email": admin["email"], "created_at": admin["created_at"]}


def create_admin_session(admin_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=ADMIN_SESSION_LIFETIME_DAYS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, admin_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, admin_id, now_iso(), expires_at),
        )
    return token


def get_admin_by_token(token: str):
    """Return the admin identity dict for a valid, non-expired admin session
    token, else None. Completely separate from get_account_by_token - an
    admin session can never be used to authenticate a customer-scoped 🔒
    endpoint, and vice versa."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT a.id AS id, a.email, a.created_at, s.expires_at
            FROM admin_sessions s
            JOIN admins a ON a.id = s.admin_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now_iso():
            conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            return None
        return dict(row)


def delete_admin_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))


def list_accounts_overview() -> list:
    """Every account with rollup counts, newest first - the support
    overview list. Deliberately simple per-account subqueries (not one big
    JOIN/GROUP BY) since the number of accounts is small and this stays easy
    to read and get right."""
    with get_conn() as conn:
        accounts = conn.execute("SELECT id, company_name, created_at FROM accounts ORDER BY created_at DESC").fetchall()
        result = []
        for acc in accounts:
            acc = dict(acc)
            acc["user_count"] = count_users(acc["id"])
            acc["contact_count"] = count_contacts(acc["id"])
            campaigns = conn.execute("SELECT COUNT(*) AS n FROM campaigns WHERE account_id = ?", (acc["id"],)).fetchone()
            acc["campaign_count"] = campaigns["n"]
            result.append(acc)
        return result


def get_account_overview_detail(account_id: int, contacts_limit: int = 200):
    """Read-mostly support view of one account: its users (no password
    hashes), a capped list of contacts, its campaigns, and LinkedIn stats.
    Returns None if the account doesn't exist. Deliberately doesn't include
    Gmail-inbox content or anything from outside this account's own tables."""
    with get_conn() as conn:
        account = conn.execute("SELECT id, company_name, created_at FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            return None
        users = conn.execute(
            "SELECT id, email, created_at FROM users WHERE account_id = ? ORDER BY created_at ASC", (account_id,)
        ).fetchall()
        contacts = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? ORDER BY created_at DESC LIMIT ?",
            (account_id, contacts_limit),
        ).fetchall()
        contact_count = count_contacts(account_id)
        campaigns = conn.execute(
            "SELECT * FROM campaigns WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
        ).fetchall()
        return {
            "account": dict(account),
            "users": [dict(u) for u in users],
            "contacts": [dict(c) for c in contacts],
            "contact_count": contact_count,
            "campaigns": [dict(c) for c in campaigns],
            "linkedin_stats": linkedin_stats(account_id),
        }


def reset_user_password_for_account(account_id: int, user_id: int, new_password: str) -> bool:
    """Support-initiated password reset for one specific user, scoped to
    make sure that user actually belongs to the given account (so a support
    person can't accidentally - or a buggy caller can't - reset a password
    on the wrong account). Returns False if no such user/account
    combination exists. Also invalidates that user's sessions, same as the
    self-service/admin resets elsewhere."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT email FROM users WHERE id = ? AND account_id = ?", (user_id, account_id)
        ).fetchone()
        if not row:
            return False
    return set_password(row["email"], new_password)


# ---------------------------------------------------------------------------
# Fase 3c (crm-roadmap.md): intelligente CSV-import.
#
# Twee stappen i.p.v. één: POST /api/contacts/import-csv/preview leest het
# bestand, stelt een kolom-koppeling voor (deterministische aliassen +
# optioneel een AI-verfijning, zie app.py/_suggest_header_mapping) en bewaart
# de ruwe headers/rijen hier onder een token; POST .../confirm haalt die
# sessie op, past de door de klant gecontroleerde/aangepaste koppeling toe en
# importeert pas dan echt. Zo hoeft een klant nooit meer handmatig
# kolomkoppen in het bronbestand te hernoemen voordat importeren lukt - de
# oude, direct-importerende /api/contacts/import-csv blijft daarnaast gewoon
# bestaan voor bestaande integraties/scripts.
# ---------------------------------------------------------------------------

def create_csv_import_session(account_id: int, filename: str, raw_headers: list,
                               raw_rows: list, suggested_mapping: dict, mapping_source: str) -> dict:
    token = new_token()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO csv_import_sessions
                (token, account_id, filename, raw_headers, raw_rows, suggested_mapping, mapping_source, row_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, account_id, filename, json.dumps(raw_headers), json.dumps(raw_rows),
             json.dumps(suggested_mapping), mapping_source, len(raw_rows), now_iso()),
        )
    return {
        "token": token, "filename": filename, "headers": raw_headers, "rows": raw_rows,
        "suggested_mapping": suggested_mapping, "mapping_source": mapping_source, "row_count": len(raw_rows),
    }


def get_csv_import_session(token: str, account_id: int) -> dict | None:
    """Scoped to account_id so one account can never confirm/read another
    account's still-pending import session, even if it somehow guessed the
    token."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM csv_import_sessions WHERE token = ? AND account_id = ?", (token, account_id)
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        row["headers"] = json.loads(row.pop("raw_headers"))
        row["rows"] = json.loads(row.pop("raw_rows"))
        row["suggested_mapping"] = json.loads(row["suggested_mapping"])
        return row


def delete_csv_import_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM csv_import_sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# Ideeenbus (crm-roadmap.md): feedback/ideeen van klanten, apart van
# support_tickets - dit is input voor de roadmap, geen supportvraag die een
# individueel antwoord verwacht (al kan het team er via `status` wel op
# reageren: nieuw -> in overweging -> op de roadmap -> gebouwd/afgewezen).
# ---------------------------------------------------------------------------

def create_feedback_item(account_id: int, user_id, category: str, message: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO feedback_items (account_id, user_id, category, message, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (account_id, user_id, category, message, now_iso()),
        )
        item_id = cur.fetchone()["id"]
        row = conn.execute("SELECT * FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row)


def list_feedback_items(account_id: int) -> list:
    """Een account ziet alleen zijn eigen ingediende ideeen/feedback (en de
    status ervan) - net als support_tickets, geen inzage in wat andere
    klanten hebben ingediend."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback_items WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_feedback_items() -> list:
    """Voor het superadmin/support-overzicht - over alle accounts heen, met
    bedrijfsnaam erbij zodat Twikey ziet van wie welk idee komt."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT f.*, a.company_name
            FROM feedback_items f
            JOIN accounts a ON a.id = f.account_id
            ORDER BY f.created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def set_feedback_status(feedback_id: int, status: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM feedback_items WHERE id = ?", (feedback_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE feedback_items SET status = ? WHERE id = ?", (status, feedback_id))
        updated = conn.execute("SELECT * FROM feedback_items WHERE id = ?", (feedback_id,)).fetchone()
        return dict(updated)


# ---------------------------------------------------------------------------
# Fase 3c (crm-roadmap.md): dagelijkse samenvatting-mail + domain warm-up
# (instelbare dagelijkse verzendlimiet).
#
# Beide instellingen leven op de accounts-tabel zelf (net als
# auto_reply_enabled) - het zijn platform-brede aan/uit-schakelaars per
# account, geen aparte tabel nodig. get_sending_settings()/
# update_sending_settings() ontsluiten ze samen omdat de instellingenkaart
# in het dashboard ze ook samen toont/bewerkt.
# ---------------------------------------------------------------------------

def get_sending_settings(account_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT daily_digest_enabled, daily_send_limit_enabled, daily_send_limit,
                   unsubscribe_link_enabled, last_digest_sent_date
            FROM accounts WHERE id = ?
            """,
            (account_id,),
        ).fetchone()
        return {
            "daily_digest_enabled": bool(row["daily_digest_enabled"]),
            "daily_send_limit_enabled": bool(row["daily_send_limit_enabled"]),
            "daily_send_limit": row["daily_send_limit"],
            "unsubscribe_link_enabled": bool(row["unsubscribe_link_enabled"]),
            "last_digest_sent_date": row["last_digest_sent_date"],
        }


def update_sending_settings(account_id: int, daily_digest_enabled: bool = None,
                             daily_send_limit_enabled: bool = None, daily_send_limit: int = None,
                             unsubscribe_link_enabled: bool = None) -> dict:
    fields, params = [], []
    if daily_digest_enabled is not None:
        fields.append("daily_digest_enabled = ?")
        params.append(1 if daily_digest_enabled else 0)
    if daily_send_limit_enabled is not None:
        fields.append("daily_send_limit_enabled = ?")
        params.append(1 if daily_send_limit_enabled else 0)
    if daily_send_limit is not None:
        fields.append("daily_send_limit = ?")
        params.append(daily_send_limit)
    if unsubscribe_link_enabled is not None:
        fields.append("unsubscribe_link_enabled = ?")
        params.append(1 if unsubscribe_link_enabled else 0)
    if fields:
        params.append(account_id)
        with get_conn() as conn:
            conn.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", params)
    return get_sending_settings(account_id)


def set_do_not_contact_by_id(contact_id: int) -> dict | None:
    """Zet do_not_contact voor één contact op basis van diens id alleen -
    GEEN account_id-scoping, want de aanroeper hier is altijd de publieke,
    ongeauthenticeerde afmeldlink (zie /track/unsubscribe/{token} in app.py),
    waar het HMAC-ondertekende token zelf al de autorisatie is (hetzelfde
    patroon als de bestaande open/click-tracking op tracking_token). Logt
    een contact_activity-event zodat het afmelden ook in de tijdlijn
    zichtbaar is. Geeft None terug als het contact niet (meer) bestaat."""
    with get_conn() as conn:
        row = conn.execute("SELECT id, account_id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE contacts SET do_not_contact = 1 WHERE id = ?", (contact_id,))
        conn.execute(
            "INSERT INTO contact_activity (account_id, contact_id, event_type, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["account_id"], contact_id, "unsubscribed", "Afgemeld via afmeldlink in een mail.", now_iso()),
        )
        updated = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(updated)


# ---------------------------------------------------------------------------
# "Aandacht nodig"-dashboard (crm-roadmap.md): een klein, samengesteld
# overzicht van dingen die actie van de klant vragen - bewust hergebruikt
# bestaande data (mislukte verzendingen, openstaande conceptantwoorden,
# vervallen herinneringen, een eventuele verzendwachtrij) i.p.v. een nieuwe
# tabel/tracking-mechanisme, zodat dit meteen werkt voor elk bestaand
# account.
# ---------------------------------------------------------------------------

def attention_items(account_id: int) -> list:
    items = []
    with get_conn() as conn:
        failed_campaigns = conn.execute(
            """
            SELECT COUNT(*) AS n FROM campaign_recipients cr
            JOIN campaigns camp ON camp.id = cr.campaign_id
            WHERE camp.account_id = ? AND cr.send_error IS NOT NULL
              AND cr.sent_at IS NULL
            """,
            (account_id,),
        ).fetchone()["n"]
        failed_sequences = conn.execute(
            """
            SELECT COUNT(*) AS n FROM sequence_sends ss
            JOIN sequence_enrollments se ON se.id = ss.enrollment_id
            WHERE se.account_id = ? AND ss.send_error IS NOT NULL AND ss.sent_at IS NULL
            """,
            (account_id,),
        ).fetchone()["n"]
        failed_total = (failed_campaigns or 0) + (failed_sequences or 0)
        if failed_total:
            items.append({
                "type": "failed_sends", "severity": "high",
                "message": f"{failed_total} mail(s) konden niet verstuurd worden - controleer je mailinstellingen.",
                "count": failed_total, "tab": "email",
            })

        pending_drafts = conn.execute(
            "SELECT COUNT(*) AS n FROM reply_drafts WHERE account_id = ? AND status = 'pending'", (account_id,)
        ).fetchone()["n"]
        if pending_drafts:
            items.append({
                "type": "pending_reply_drafts", "severity": "medium",
                "message": f"{pending_drafts} conceptantwoord(en) wachten op jouw goedkeuring.",
                "count": pending_drafts, "tab": "replies",
            })

        due_reminders_count = conn.execute(
            "SELECT COUNT(*) AS n FROM reminders WHERE account_id = ? AND status = 'open' AND remind_at <= ?",
            (account_id, now_iso()),
        ).fetchone()["n"]
        if due_reminders_count:
            items.append({
                "type": "due_reminders", "severity": "medium",
                "message": f"{due_reminders_count} herinnering(en) staan open om weer contact op te nemen.",
                "count": due_reminders_count, "tab": "contacts",
            })

    queued = len(pending_campaign_recipients_for_account(account_id, limit=100000))
    if queued:
        items.append({
            "type": "campaign_queue", "severity": "low",
            "message": f"{queued} campagne-mail(s) staan in de wachtrij door de dagelijkse verzendlimiet.",
            "count": queued, "tab": "abtest",
        })

    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: order.get(i["severity"], 9))
    return items


def _utc_day_start(now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def emails_sent_today(account_id: int, day_start_iso: str = None) -> int:
    """Telt alle daadwerkelijk verzonden mails (campagnes + opvolgsequenties)
    voor dit account sinds day_start_iso (standaard: middernacht UTC vandaag)
    - de teller achter de dagelijkse verzendlimiet (domain warm-up, zie
    remaining_daily_budget()). Mislukte verzendpogingen tellen niet mee -
    alleen wat daadwerkelijk de deur uit is gegaan raakt het domein."""
    day_start_iso = day_start_iso or _utc_day_start()
    with get_conn() as conn:
        campaign_count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM campaign_recipients cr
            JOIN campaigns camp ON camp.id = cr.campaign_id
            WHERE camp.account_id = ? AND cr.sent_at >= ?
            """,
            (account_id, day_start_iso),
        ).fetchone()["n"]
        sequence_count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM sequence_sends ss
            JOIN sequence_enrollments se ON se.id = ss.enrollment_id
            WHERE se.account_id = ? AND ss.sent_at >= ?
            """,
            (account_id, day_start_iso),
        ).fetchone()["n"]
        return (campaign_count or 0) + (sequence_count or 0)


def remaining_daily_budget(account_id: int) -> int | None:
    """None = geen limiet (uitgeschakeld - standaard). Anders het aantal
    mails dat dit account vandaag (nog) mag versturen, nooit negatief."""
    settings = get_sending_settings(account_id)
    if not settings["daily_send_limit_enabled"]:
        return None
    return max(0, settings["daily_send_limit"] - emails_sent_today(account_id))


def pending_campaign_recipients_for_account(account_id: int, limit: int) -> list:
    """Ontvangers van (al gelanceerde) campagnes van dit account die nog
    NOOIT geprobeerd zijn te versturen (sent_at en send_error allebei leeg) -
    dat is precies de wachtrij die ontstaat als een campagne-launch werd
    afgekapt door de dagelijkse verzendlimiet (zie api_launch_campaign in
    app.py). Oudste campagne/ontvanger eerst, zodat een wachtrij op volgorde
    wordt weggewerkt zodra er weer ruimte in het dagbudget is."""
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
            WHERE camp.account_id = ? AND camp.status = 'launched'
              AND cr.sent_at IS NULL AND cr.send_error IS NULL
            ORDER BY camp.launched_at ASC, cr.id ASC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def account_ids_with_pending_campaign_sends() -> list:
    """Across all accounts - used by the campaign-queue cron so it only has
    to compute a remaining-budget check for accounts that actually have
    something waiting, instead of looping over every account every run."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT camp.account_id AS account_id
            FROM campaign_recipients cr
            JOIN campaigns camp ON camp.id = cr.campaign_id
            WHERE camp.status = 'launched' AND cr.sent_at IS NULL AND cr.send_error IS NULL
            """
        ).fetchall()
        return [r["account_id"] for r in rows]


def accounts_needing_digest() -> list:
    """Alle accounts met daily_digest_enabled=1 die vandaag (UTC) nog geen
    samenvatting-mail hebben gehad - idempotent ongeacht hoe vaak de cron
    draait (zie POST /api/cron/process-digests)."""
    today = _utc_today()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, company_name FROM accounts
            WHERE daily_digest_enabled = 1
              AND (last_digest_sent_date IS NULL OR last_digest_sent_date <> ?)
            """,
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_digest_sent(account_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET last_digest_sent_date = ? WHERE id = ?", (_utc_today(), account_id))


def digest_stats(account_id: int, since_iso: str = None) -> dict:
    """Activiteiten/resultaten van de afgelopen 24 uur (of sinds since_iso)
    voor de dagelijkse samenvatting-mail: verzonden mails (campagnes +
    sequenties), opens, clicks, nieuwe replies en het aantal actieve
    campagnes/sequenties - dezelfde brondata als de Analytics-tab, alleen
    over een vast tijdvenster i.p.v. all-time."""
    since_iso = since_iso or (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with get_conn() as conn:
        camp = conn.execute(
            """
            SELECT
                SUM(CASE WHEN cr.sent_at >= ? THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN cr.opened_at >= ? THEN 1 ELSE 0 END) AS opens,
                SUM(CASE WHEN cr.clicked_at >= ? THEN 1 ELSE 0 END) AS clicks
            FROM campaign_recipients cr
            JOIN campaigns camp ON camp.id = cr.campaign_id
            WHERE camp.account_id = ?
            """,
            (since_iso, since_iso, since_iso, account_id),
        ).fetchone()
        seq_sent = conn.execute(
            """
            SELECT SUM(CASE WHEN ss.sent_at >= ? THEN 1 ELSE 0 END) AS sent
            FROM sequence_sends ss
            JOIN sequence_enrollments se ON se.id = ss.enrollment_id
            WHERE se.account_id = ?
            """,
            (since_iso, account_id),
        ).fetchone()
        new_replies = conn.execute(
            "SELECT COUNT(*) AS n FROM incoming_replies WHERE account_id = ? AND received_at >= ?",
            (account_id, since_iso),
        ).fetchone()
        active_campaigns = conn.execute(
            "SELECT COUNT(*) AS n FROM campaigns WHERE account_id = ? AND status = 'launched'", (account_id,)
        ).fetchone()
        active_sequences = conn.execute(
            "SELECT COUNT(*) AS n FROM sequences WHERE account_id = ? AND status = 'active'", (account_id,)
        ).fetchone()
        return {
            "emails_sent": (camp["sent"] or 0) + (seq_sent["sent"] or 0),
            "opens": camp["opens"] or 0,
            "clicks": camp["clicks"] or 0,
            "new_replies": new_replies["n"],
            "active_campaigns": active_campaigns["n"],
            "active_sequences": active_sequences["n"],
        }


# ---------------------------------------------------------------------------
# Campagne-overzicht (crm-roadmap.md): één rij per campagne met verzonden/
# opens/clicks/replies/conversie - anders dan campaign_results() (dat gaat
# per VARIANT binnen één campagne), dit is het overzicht over ALLE campagnes
# van een account heen voor de nieuwe "Campagne-overzicht"-kaart.
# ---------------------------------------------------------------------------

def campaigns_overview(account_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT camp.id, camp.name, camp.status, camp.created_at, camp.launched_at,
                   COUNT(cr.id) AS total_recipients,
                   SUM(CASE WHEN cr.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN cr.send_error IS NOT NULL THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN cr.opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opens,
                   SUM(CASE WHEN cr.clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicks
            FROM campaigns camp
            LEFT JOIN campaign_recipients cr ON cr.campaign_id = camp.id
            WHERE camp.account_id = ?
            GROUP BY camp.id
            ORDER BY camp.created_at DESC
            """,
            (account_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            reply_row = conn.execute(
                """
                SELECT COUNT(DISTINCT ir.contact_id) AS n
                FROM incoming_replies ir
                JOIN campaign_recipients cr ON cr.contact_id = ir.contact_id
                WHERE cr.campaign_id = ? AND ir.account_id = ?
                """,
                (d["id"], account_id),
            ).fetchone()
            sent = d["sent"] or 0
            d["replies"] = reply_row["n"] or 0
            d["pending"] = max(0, (d["total_recipients"] or 0) - sent - (d["failed"] or 0))
            d["conversion_rate"] = round(d["replies"] / sent, 3) if sent else 0.0
            result.append(d)
        return result
