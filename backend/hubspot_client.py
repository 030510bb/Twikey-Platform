"""
HubSpot CRM client (Fase 2, crm-roadmap.md) - live customer/open-deal
exclusion check by company domain, one of the three uitsluitlijst
mechanisms described in crm-roadmap.md's architecture section.

Per-account private-app access token - see hubspot_settings in database.py.
Every customer connects their OWN HubSpot account; this is unrelated to
this coding session's own Vibe Prospecting/HubSpot chat tools, which only
work here in the chat, not in the live dashboard (see crm-roadmap.md).

Required scopes on the customer's HubSpot private app:
  - crm.objects.companies.read
  - crm.objects.deals.read
"""

import requests

BASE_URL = "https://api.hubapi.com"
REQUEST_TIMEOUT = 15


class HubspotError(Exception):
    pass


def _request(token: str, method: str, path: str, json_body: dict = None) -> dict:
    try:
        resp = requests.request(
            method, f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json_body, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HubspotError(f"Kon HubSpot niet bereiken: {exc}") from exc

    if resp.status_code == 401:
        raise HubspotError("Ongeldig of verlopen HubSpot access token.")
    if resp.status_code == 403:
        raise HubspotError(
            "HubSpot access token mist de benodigde scopes "
            "(crm.objects.companies.read / crm.objects.deals.read)."
        )
    if not resp.ok:
        raise HubspotError(f"HubSpot gaf een fout terug ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise HubspotError("Onverwacht antwoord van HubSpot (geen geldige JSON).") from exc


def find_company_by_domain(token: str, domain: str) -> dict | None:
    data = _request(token, "POST", "/crm/v3/objects/companies/search", {
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["name", "domain", "lifecyclestage"],
        "limit": 1,
    })
    results = data.get("results") or []
    return results[0] if results else None


def has_open_deal_for_company(token: str, company_id: str) -> bool:
    """An "open" deal is any associated deal not in a closed-won/closed-lost
    stage. HubSpot's default pipeline uses stage ids containing
    "closedwon"/"closedlost"; custom pipelines vary but conventionally keep
    those substrings, which is what we match on here."""
    data = _request(token, "GET", f"/crm/v3/objects/companies/{company_id}/associations/deals")
    deal_ids = [r["id"] for r in (data.get("results") or [])]
    if not deal_ids:
        return False
    for deal_id in deal_ids:
        deal = _request(token, "GET", f"/crm/v3/objects/deals/{deal_id}?properties=dealstage")
        stage = ((deal.get("properties") or {}).get("dealstage") or "").lower()
        if stage and "closedwon" not in stage and "closedlost" not in stage:
            return True
    return False


def check_domain(token: str, domain: str, check_customer: bool = True, check_open_deal: bool = True) -> dict:
    """Returns {} if the domain doesn't exist as a HubSpot company at all
    (nothing to exclude on). Otherwise
    {"company_name", "is_customer", "has_open_deal"} - only the flags that
    were asked for are actually checked (has_open_deal costs extra API
    calls, skip it when the account only cares about existing customers)."""
    company = find_company_by_domain(token, domain)
    if not company:
        return {}
    props = company.get("properties") or {}
    result = {"company_name": props.get("name") or "", "is_customer": False, "has_open_deal": False}
    if check_customer:
        result["is_customer"] = (props.get("lifecyclestage") or "").lower() == "customer"
    if check_open_deal:
        result["has_open_deal"] = has_open_deal_for_company(token, company["id"])
    return result
