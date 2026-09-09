"""
Vibe Prospecting / Explorium client (Fase 2, crm-roadmap.md punt 3 + Fase 2
"Vibe Prospecting/Explorium: echte zoek- en lookalike-endpoints").

Per-account "bring your own key" - see prospecting_settings in database.py.
Talks directly to Explorium's v2 REST API (developers.explorium.ai) - there
is no official Python SDK, so this is a thin `requests` wrapper using their
documented `api_key` header auth (a plain header, not a Bearer token).

Endpoints used here (per crm-roadmap.md's "Openstaande vragen" note - these
are the documented v2 endpoints, not yet exercised against a live customer
key since no customer has connected one yet; the account settings screen
deliberately allows saving without one, see database.py's
prospecting_settings comment):
  - POST /v2/businesses/match   - find a business by name/domain, returns a business_id
  - POST /v2/businesses/search  - filter-based business search, used here for lookalikes
  - POST /v2/prospects/match    - find people at a matched business
  - POST /v2/prospects/contact_information/enrich - email/phone for matched prospects
       (2 credits/email, 5/phone per Explorium's pricing)

Every call raises ExploriumError with a readable, Dutch message on any
non-2xx response so app.py can surface it to the customer instead of a raw
traceback. Response field names below (matched_businesses/data etc.) are
best-effort against Explorium's published docs - worth a quick sanity check
against the real response shape the first time an account connects a real
key, since that hasn't happened yet.
"""

import requests

BASE_URL = "https://api.explorium.ai"
REQUEST_TIMEOUT = 20


class ExploriumError(Exception):
    pass


def _request(api_key: str, method: str, path: str, json_body: dict = None) -> dict:
    try:
        resp = requests.request(
            method, f"{BASE_URL}{path}",
            headers={"api_key": api_key, "Content-Type": "application/json"},
            json=json_body, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ExploriumError(f"Kon Explorium niet bereiken: {exc}") from exc

    if resp.status_code in (401, 403):
        raise ExploriumError("Ongeldige of verlopen Explorium API-key.")
    if resp.status_code == 429:
        raise ExploriumError("Explorium rate limit bereikt (max 200 verzoeken/minuut) - probeer het straks opnieuw.")
    if not resp.ok:
        raise ExploriumError(f"Explorium gaf een fout terug ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ExploriumError("Onverwacht antwoord van Explorium (geen geldige JSON).") from exc


def match_businesses(api_key: str, businesses: list) -> list:
    """businesses: [{"name": ..., "domain": ...}, ...], at least one of
    name/domain per entry. Returns the matched business records
    (including a business_id used by the other functions below)."""
    data = _request(api_key, "POST", "/v2/businesses/match", {"businesses_to_match": businesses})
    return data.get("matched_businesses") or data.get("data") or []


def search_lookalike_businesses(api_key: str, business_id: str, size: int = 20) -> list:
    """Lookalikes (crm-roadmap.md punt 3) - a business-search filtered to
    companies similar to an already-matched one."""
    data = _request(api_key, "POST", "/v2/businesses/search", {
        "mode": "full",
        "size": size,
        "filters": {"linkedin_similar_companies": [business_id]},
    })
    return data.get("data") or []


def match_prospects(api_key: str, prospects: list) -> list:
    """prospects: [{"business_id": ..., "job_titles": [...]}] or
    [{"full_name": ..., "company_name": ...}], etc."""
    data = _request(api_key, "POST", "/v2/prospects/match", {"prospects_to_match": prospects})
    return data.get("matched_prospects") or data.get("data") or []


def enrich_prospect_contacts(api_key: str, prospect_ids: list) -> list:
    data = _request(api_key, "POST", "/v2/prospects/contact_information/enrich", {
        "prospect_ids": prospect_ids,
    })
    return data.get("data") or []
