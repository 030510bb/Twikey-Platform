"""
Meta (Facebook/Instagram) Lead Ads client - crm-roadmap.md, "leads uit
Instagram/LinkedIn-advertenties". Polls Meta's Graph API for new Lead Ads
form submissions (Bulk Read, no webhook) on specific Lead Gen Forms.

Auth: same "paste a long-lived credential" pattern as hubspot_client.py -
no interactive OAuth consent flow built here. The customer generates a
long-lived Page/System User access token via Meta Business Settings
(recommended: a System User token, which doesn't expire) for an app that
has been through Meta's App Review for the leads_retrieval permission -
see crm-roadmap.md for what that requires. Paste that token plus the
Lead Gen Form ID(s) to watch (comma-separated).

Endpoint shapes verified against Meta's official docs
(developers.facebook.com/documentation/ads-commerce/marketing-api/guides/
lead-ads/retrieving, retrieved 14 sept 2026) - NOT yet tested against a
real ad account, since no approved Meta app/leads_retrieval access exists
for this platform yet. Treat this module as a solid first draft, not a
verified integration. Meta only retains lead data for 90 days, so
whatever cron calls fetch_new_leads() needs to run at least that often to
never miss a lead.
"""

import requests

GRAPH_API_VERSION = "v25.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
REQUEST_TIMEOUT = 15

# Meta lead form field name -> contact dict key. These are the standard
# field keys Meta uses for its own predefined form questions; a custom
# question's name won't match anything here and is simply skipped.
_FIELD_MAP = {
    "email": "email",
    "first_name": "first_name",
    "last_name": "last_name",
    "full_name": "full_name",
    "company_name": "company",
    "job_title": "job_title",
}


class MetaAdsError(Exception):
    pass


def _get(path: str, access_token: str, params: dict = None) -> dict:
    query = {"access_token": access_token, **(params or {})}
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=query, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise MetaAdsError(f"Kon Meta niet bereiken: {exc}") from exc
    if resp.status_code >= 400:
        raise MetaAdsError(f"Meta API-fout ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _normalize_lead(raw: dict) -> dict:
    contact = {"lead_id": raw.get("id"), "created_time": raw.get("created_time"), "form_id": raw.get("form_id")}
    for field in raw.get("field_data") or []:
        contact_key = _FIELD_MAP.get((field.get("name") or "").lower())
        values = field.get("values") or []
        if contact_key and values:
            contact[contact_key] = (values[0] or "").strip()
    if not contact.get("first_name") and contact.get("full_name"):
        parts = contact["full_name"].split(" ", 1)
        contact["first_name"] = parts[0]
        contact["last_name"] = contact.get("last_name") or (parts[1] if len(parts) > 1 else "")
    return contact


def fetch_new_leads(access_token: str, form_ids: list, since_iso: str = None) -> list:
    """Leads sinds since_iso (ISO-tijdstip, vergelijkbaar met Meta's eigen
    created_time-formaat, None = alles) voor de gegeven Lead Gen Form-ID's,
    genormaliseerd naar {lead_id, created_time, form_id, first_name,
    last_name, email, company, job_title}. Doorloopt de cursor-paginering
    volledig, per formulier."""
    leads = []
    for form_id in form_ids:
        params = {"fields": "created_time,id,ad_id,form_id,field_data", "limit": 100}
        after = None
        while True:
            if after:
                params["after"] = after
            data = _get(f"/{form_id}/leads", access_token, params)
            for raw in data.get("data", []):
                created_time = raw.get("created_time")
                if since_iso and created_time and created_time <= since_iso:
                    continue
                leads.append(_normalize_lead(raw))
            paging = data.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not after or not paging.get("next"):
                break
    return leads
