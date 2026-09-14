"""
LinkedIn Ads (Lead Sync API) client - crm-roadmap.md, "leads uit
Instagram/LinkedIn-advertenties". Polls LinkedIn's leadFormResponses
endpoint for new Lead Gen Form submissions on a connected Sponsored (paid
ads) account, and normalizes them into plain contact fields.

Auth: NOT a full interactive OAuth consent flow - same "paste a
long-lived credential" pattern this codebase already uses for
HubSpot/Explorium (see hubspot_client.py/prospecting_client.py). The
customer generates an access token via LinkedIn's Developer Portal for
their own app (which must have Lead Sync API product access, granted via
LinkedIn's App Review - see crm-roadmap.md for what that requires) and
pastes it in here, together with their Sponsored Account URN. LinkedIn
access tokens expire (roughly 60 days) and currently need to be manually
refreshed/re-pasted when that happens - a refresh-token flow can be added
later once this has been validated against a real connected account.

Endpoint shapes verified against LinkedIn's official docs
(learn.microsoft.com/en-us/linkedin/marketing/lead-sync/leadsync,
retrieved 14 sept 2026) - NOT yet tested against a real account, since no
approved LinkedIn app/Lead Sync API access exists for this platform yet.
Treat this module as a solid first draft, not a verified integration.
"""

import re

import requests

BASE_URL = "https://api.linkedin.com/rest"
API_VERSION = "202508"  # LinkedIn-Version header (YYYYMM) - bump periodically
REQUEST_TIMEOUT = 15

# leadForms question predefinedField -> contact dict key. Only the
# predefined fields relevant to importing a contact are mapped; anything
# else (custom questions, consent checkboxes) is ignored.
_FIELD_MAP = {
    "FIRST_NAME": "first_name",
    "LAST_NAME": "last_name",
    "EMAIL": "email",
    "COMPANY_NAME": "company",
    "JOB_TITLE": "job_title",
}


class LinkedInAdsError(Exception):
    pass


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": API_VERSION,
    }


def _request(access_token: str, path: str, params: dict) -> dict:
    try:
        resp = requests.get(f"{BASE_URL}{path}", headers=_headers(access_token), params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise LinkedInAdsError(f"Kon LinkedIn niet bereiken: {exc}") from exc
    if resp.status_code >= 400:
        raise LinkedInAdsError(f"LinkedIn API-fout ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _form_id_from_urn(versioned_urn: str) -> str | None:
    """'urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,1)' -> '3162'."""
    match = re.search(r"leadGenForm:(\d+)", versioned_urn or "")
    return match.group(1) if match else None


_form_field_cache: dict = {}


def _field_map_for_form(access_token: str, form_id: str) -> dict:
    """questionId -> predefinedField for one form, cached for the lifetime
    of this process - a form's questions don't change often, and this
    avoids an extra API call per lead that reuses the same form."""
    if form_id in _form_field_cache:
        return _form_field_cache[form_id]
    data = _request(access_token, f"/leadForms/{form_id}", {})
    field_map = {}
    for q in data.get("questions", []) or []:
        predefined = q.get("predefinedField")
        question_id = q.get("questionId")
        if predefined and question_id is not None:
            field_map[question_id] = predefined
    _form_field_cache[form_id] = field_map
    return field_map


def _extract_answer_text(answer: dict) -> str:
    details = answer.get("answerDetails") or {}
    text = details.get("textQuestionAnswer", {}).get("answer")
    return (text or "").strip()


def fetch_new_leads(access_token: str, sponsored_account_urn: str, since_ms: int = None) -> list:
    """Sponsored (betaalde advertenties) Lead Gen Form-inzendingen sinds
    since_ms (Unix epoch in milliseconden, None = alles), genormaliseerd
    naar {lead_id, submitted_at_ms, first_name, last_name, email, company,
    job_title} - een veld dat niet als predefined field in het formulier
    zat blijft leeg. Beperkt tot SPONSORED leads (betaalde advertenties);
    organische/Event-leads worden hier bewust niet opgehaald."""
    params = {
        "q": "owner",
        "owner": f"(sponsoredAccount:{sponsored_account_urn})",
        "leadType": "(leadType:SPONSORED)",
        "count": 100,
    }
    if since_ms is not None:
        params["submittedAtTimeRange"] = f"(start:{since_ms})"
    data = _request(access_token, "/leadFormResponses", params)

    leads = []
    for element in data.get("elements", []):
        form_id = _form_id_from_urn(element.get("versionedLeadGenFormUrn"))
        field_map = _field_map_for_form(access_token, form_id) if form_id else {}
        contact = {"lead_id": element.get("id"), "submitted_at_ms": element.get("submittedAt")}
        for answer in (element.get("formResponse") or {}).get("answers", []):
            predefined = field_map.get(answer.get("questionId"))
            contact_key = _FIELD_MAP.get(predefined)
            if contact_key:
                contact[contact_key] = _extract_answer_text(answer)
        leads.append(contact)
    return leads
