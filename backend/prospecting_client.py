"""
Vibe Prospecting / Explorium client (Fase 2, crm-roadmap.md punt 3 + Fase 2
"Vibe Prospecting/Explorium: echte zoek- en lookalike-endpoints").

Per-account "bring your own key" - see prospecting_settings in database.py.
Talks directly to Explorium's v1 REST API (developers.explorium.ai) - there
is no official Python SDK, so this is a thin `requests` wrapper using their
documented `API_KEY` header auth (a plain header, not a Bearer token).

Endpoints used here - verified against developers.explorium.ai on
2026-09-14 (the first time an account actually connected a live key and
every path/shape below turned out to differ from what this module
originally assumed - a stale /v2/... base path, a batched enrich call that
Explorium only ever supported one-prospect-at-a-time, and a "match
prospects by business_id" call that isn't what that endpoint is for at
all, see fetch_prospects() below):
  - POST /v1/businesses/match                    - find a business by name/domain, returns a business_id
  - POST /v1/businesses                          - filter-based business search (sector search)
  - POST /v1/businesses/lookalikes/enrich         - lookalikes for ONE business_id (NOT a /v1/businesses filter -
                                                     linkedin_similar_companies as a `filters` field gives a 422
                                                     "extra fields not permitted", discovered 14 sept 2026 against
                                                     a real key; this is a dedicated enrichment endpoint with its
                                                     own lookalike_*-prefixed response shape, normalized below)
  - POST /v1/prospects/match                     - match ONE already-identified person (by email/phone/linkedin/full_name+company)
  - POST /v1/prospects                           - filter-based prospect search (e.g. business_id + job_title) - this is
                                                     the one to use for "find people at this company", not prospects/match
  - POST /v1/prospects/contacts_information/enrich - email/phone for ONE prospect_id per call (not batched)
       (2 credits/email, 5/phone per Explorium's pricing)

Every call raises ExploriumError with a readable, Dutch message on any
non-2xx response so app.py can surface it to the customer instead of a raw
traceback.
"""

from urllib.parse import urlparse

import requests

BASE_URL = "https://api.explorium.ai"
REQUEST_TIMEOUT = 20


class ExploriumError(Exception):
    pass


def _request(api_key: str, method: str, path: str, json_body: dict = None) -> dict:
    try:
        resp = requests.request(
            method, f"{BASE_URL}{path}",
            headers={"API_KEY": api_key, "Content-Type": "application/json"},
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
    data = _request(api_key, "POST", "/v1/businesses/match", {"businesses_to_match": businesses})
    return data.get("matched_businesses") or data.get("data") or []


def search_businesses(api_key: str, filters: dict, size: int = 20) -> list:
    """Generieke filter-based business search (POST /v1/businesses) -
    filters is een Explorium filter-dict met per veld een {"values": [...]}
    object, bv. {"linkedin_category": {"values": [...]}} voor sector-zoeken
    (dagelijkse prospecting-cron). page_size is een verplicht veld bij
    Explorium (geeft anders een 422 "field required"). Voor lookalikes NIET
    hier een filter aan meegeven - dat is een apart endpoint, zie
    search_lookalike_businesses()."""
    data = _request(api_key, "POST", "/v1/businesses", {
        "mode": "full",
        "size": size,
        "page_size": min(size, 100),
        "page": 1,
        "filters": filters,
    })
    return data.get("data") or []


def search_lookalike_businesses(api_key: str, business_id: str, size: int = 20) -> list:
    """Lookalikes (crm-roadmap.md punt 3) - POST /v1/businesses/lookalikes/enrich,
    een losse enrichment-call per business_id (geen `size`/paginering op
    Explorium's kant; levert doorgaans een handvol resultaten per aanroep).
    `size` knipt het resultaat aan onze kant af zodat de caller niet meer
    terugkrijgt dan gevraagd. Explorium's respons gebruikt lookalike_*-
    voorvoegsels (lookalike_business_id/lookalike_business_name/
    lookalike_website) - genormaliseerd naar business_id/name/domain zodat
    de rest van de codebase (en de frontend) dit als een gewoon
    business-record kan behandelen, net als search_businesses()."""
    data = _request(api_key, "POST", "/v1/businesses/lookalikes/enrich", {"business_id": business_id})
    results = data.get("data") or []
    businesses = []
    for r in results[:size]:
        website = r.get("lookalike_website") or ""
        domain = urlparse(website).netloc or website
        businesses.append({
            "business_id": r.get("lookalike_business_id"),
            "name": r.get("lookalike_business_name"),
            "domain": domain,
            "number_of_employees_range": r.get("lookalike_number_of_employees_range"),
            "revenue_range": r.get("lookalike_revenue_range"),
            "country": r.get("lookalike_country_location"),
            "similarity_score": r.get("similarity_score"),
        })
    return businesses


def match_prospects(api_key: str, prospects: list) -> list:
    """Matcht een AL BEKEND, specifiek persoon (bv. via e-mail/telefoon/
    linkedin/full_name+company_name) tegen Explorium's database - dit is
    GEEN zoek-/discovery-endpoint (een kaal business_id is hier niet
    genoeg om iemand mee te matchen). Voor "vind mensen bij dit bedrijf"
    is fetch_prospects() hieronder het juiste endpoint.
    prospects: [{"full_name": ..., "company_name": ...}] of
    [{"email": ...}] / [{"phone_number": ...}] / [{"linkedin": ...}] /
    [{"business_id": ...}] (dat laatste matcht op zichzelf zelden iets
    zinnigs, zie hierboven)."""
    data = _request(api_key, "POST", "/v1/prospects/match", {"prospects_to_match": prospects})
    return data.get("matched_prospects") or data.get("data") or []


def fetch_prospects(api_key: str, filters: dict, size: int = 20) -> list:
    """Filter-based prospect search (POST /v1/prospects) - het juiste
    endpoint om mensen bij een bedrijf te VINDEN (i.t.t. match_prospects
    hierboven, dat een al bekend persoon bevestigt). filters bv.
    {"business_id": {"values": [id]}, "job_title": {"values": [...],
    "include_related_job_titles": True}}. Geeft records met o.a.
    prospect_id/first_name/last_name/full_name/job_title/business_id
    terug - dus i.t.t. match_prospects/enrich_prospect_contacts hoef je
    voor naam/functie niet nog een aparte aanroep te doen. page_size is
    een verplicht veld bij Explorium (geeft anders een 422 "field
    required")."""
    data = _request(api_key, "POST", "/v1/prospects", {
        "mode": "full",
        "size": size,
        "page_size": min(size, 100),
        "page": 1,
        "filters": filters,
    })
    return data.get("data") or []


def _first_dict_value(items) -> str:
    """Explorium's enrich-response geeft e-mails/telefoonnummers terug als
    een array van objecten met een niet in de documentatie vastgelegde
    sleutelnaam (bv. {"email_address_key": "..."}) - dit pakt gewoon de
    eerste waarde uit het eerste object, ongeacht hoe die sleutel heet."""
    if not items:
        return ""
    first = items[0]
    if isinstance(first, dict) and first:
        return next(iter(first.values()), "") or ""
    return str(first) if first else ""


def enrich_prospect_contacts(api_key: str, prospect_ids: list) -> list:
    """Explorium's enrich-endpoint neemt één prospect_id per aanroep (niet
    gebatcht, ondanks wat de naam 'contacts_information' doet vermoeden) -
    dit itereert intern zodat roepers een simpele lijst-in/lijst-uit
    contract behouden. Geeft per id {"prospect_id", "email", "phone"}
    terug (lege strings als er geen contactgegevens beschikbaar zijn)."""
    results = []
    for prospect_id in prospect_ids:
        data = _request(api_key, "POST", "/v1/prospects/contacts_information/enrich", {
            "prospect_id": prospect_id,
            "parameters": {"contact_types": ["email", "phone"]},
        })
        payload = data.get("data") or {}
        results.append({
            "prospect_id": prospect_id,
            "email": _first_dict_value(payload.get("emails")),
            "phone": _first_dict_value(payload.get("phone_numbers")),
        })
    return results
