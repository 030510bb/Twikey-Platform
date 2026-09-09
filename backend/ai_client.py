"""
AI-conceptantwoorden via Claude (Fase 2, crm-roadmap.md punt 6).

Platform-level Anthropic API key (ANTHROPIC_API_KEY env var) - not
per-account. Per crm-roadmap.md "Openstaande vragen voor Fase 2": a small
number of generations per incoming reply is a low, predictable cost
compared to Explorium's per-search credits, so this is Benjamin's own key,
shared across every customer account - the same way SEND_AS_EMAIL is a
shared sender by default (an account can still override its own send path
via SMTP, but AI drafting has no per-account equivalent, on purpose, to
keep this simple).

This module only ever drafts text - it never sends anything itself. Sending
always goes through the human-approval flow in app.py (POST
/api/replies/drafts/{id}/approve), unless an account has explicitly turned
on `auto_reply_enabled` (default OFF, see crm-roadmap.md punt 6), in which
case app.py sends via the normal per-account send path right after this
returns - never from inside this module.
"""

import os

_MODEL = os.environ.get("ANTHROPIC_DRAFT_MODEL", "claude-sonnet-4-5-20250929")


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def draft_reply(reply_text: str, objection_category: str = "", suggested_reply: str = "",
                 contact_name: str = "", company_name: str = "") -> str:
    """Returns a short, ready-to-review concept reply in Dutch, personalized
    to the actual incoming message. Raises on any API failure (missing/bad
    key, network, rate limit) - callers should catch that and fall back to
    the plain objection-template suggestion, same defensive pattern as
    every other external-service call in this project (smtp_client,
    imap_client, prospecting_client, hubspot_client)."""
    import anthropic  # imported lazily so the rest of the app works even if the package/key is missing

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = (
        "Je helpt een Nederlandse sales-medewerker bij het opstellen van een kort, "
        "vriendelijk en professioneel conceptantwoord op een binnengekomen reactie "
        "op een outreach-mail. Schrijf in het Nederlands, informeel-zakelijk "
        "(je-vorm), maximaal 120 woorden. Schrijf alleen de kern van het bericht - "
        "geen aanhef ('Beste ...') en geen afsluiting met naam, dat voegt de "
        "medewerker er zelf aan toe voordat hij verstuurt. Gebruik de meegegeven "
        "bezwaar-categorie en voorbeeldreactie als leidraad, maar personaliseer "
        "op basis van de daadwerkelijke tekst van de binnengekomen reply - kopieer "
        "de voorbeeldreactie niet klakkeloos."
    )
    user_parts = [f'Ontvangen reply:\n"""\n{reply_text}\n"""']
    if objection_category:
        user_parts.append(f"Herkende bezwaar-categorie: {objection_category}")
    if suggested_reply:
        user_parts.append(f"Standaard-suggestie voor deze categorie (als inspiratie, niet om over te typen):\n{suggested_reply}")
    if contact_name:
        user_parts.append(f"Naam van de contactpersoon: {contact_name}")
    if company_name:
        user_parts.append(f"Bedrijf: {company_name}")
    user_parts.append("Schrijf het conceptantwoord.")

    message = client.messages.create(
        model=_MODEL,
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
    )
    return "".join(block.text for block in message.content if block.type == "text").strip()
