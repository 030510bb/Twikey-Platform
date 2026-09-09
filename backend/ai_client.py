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


# ---------------------------------------------------------------------------
# Fase 3b (crm-roadmap.md): AI-gedreven intake & mailsuggesties.
#
# Same division of labour as draft_reply() above: this module only ever
# talks to Claude and RAISES on any failure (missing/bad key, network,
# rate limit, unparsable response) - it never decides what to show the user
# when that happens. app.py catches these exceptions and falls back to a
# static, non-AI alternative (same defensive pattern as
# _categorize_reply/draft_reply for incoming replies), so both endpoints
# keep working with zero external dependencies even without an
# ANTHROPIC_API_KEY - the same guarantee the rest of this project makes.
# ---------------------------------------------------------------------------

def _text_of(message) -> str:
    return "".join(block.text for block in message.content if block.type == "text").strip()


def _profile_context(value_proposition: str, usps: list, personas: list = None) -> str:
    parts = [f"Waardepropositie:\n{value_proposition or '(nog niet ingevuld)'}"]
    if usps:
        parts.append("USP's:\n" + "\n".join(f"- {u}" for u in usps))
    if personas:
        persona_lines = []
        for p in personas:
            name = p.get("name") if isinstance(p, dict) else str(p)
            desc = p.get("description") if isinstance(p, dict) else ""
            persona_lines.append(f"- {name}" + (f": {desc}" if desc else ""))
        parts.append("Buyer persona's:\n" + "\n".join(persona_lines))
    return "\n\n".join(parts)


def generate_profile_questions(value_proposition: str, usps: list, personas: list = None) -> list:
    """Eén AI-verdiepingsronde (bewust geen doorlopend chatgesprek, zie
    crm-roadmap.md): geeft 2-4 gerichte vervolgvragen terug om vage of
    onvolledige antwoorden in het intakeformulier aan te scherpen."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = (
        "Je helpt een B2B sales-team hun intakeformulier aan te scherpen. Op basis "
        "van hun (mogelijk nog vage of onvolledige) waardepropositie, USP's en "
        "buyer persona's, stel je 2 tot 4 korte, concrete vervolgvragen die hen "
        "helpen dit scherper te krijgen - bijvoorbeeld ontbrekende cijfers/bewijs, "
        "een te vage USP, of een buyer persona zonder duidelijk pijnpunt. Schrijf "
        "in het Nederlands. Geef ALLEEN de vragen terug, één per regel, elk "
        "beginnend met '- ', zonder inleiding, nummering of afsluiting."
    )
    message = client.messages.create(
        model=_MODEL,
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": _profile_context(value_proposition, usps, personas)}],
    )
    lines = _text_of(message).splitlines()
    questions = [line.strip().lstrip("-").strip() for line in lines if line.strip().lstrip("-").strip()]
    if not questions:
        raise ValueError("Claude gaf geen bruikbare vragen terug")
    return questions[:4]


def generate_variant_suggestions(value_proposition: str, usps: list, persona: dict = None, count: int = 2) -> list:
    """Genereert `count` mail-variant-suggesties (offer_name/subject_template/
    body_template, met {{firstName}}/{{lastName}}/{{company}} merge-velden)
    op basis van het bedrijfsprofiel, optioneel toegespitst op één buyer
    persona - vervangt/vult aan op de vaste 4 lead-magnet varianten
    (crm-roadmap.md Fase 3: 'uitgaande mails voorgesteld o.b.v. dit profiel')."""
    import json

    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = (
        "Je schrijft korte, Nederlandse eerste-contact outreach-mails (informeel-"
        "zakelijk, je-vorm) voor een B2B sales-team, op basis van hun "
        "waardepropositie en USP's. Gebruik de merge-velden {{firstName}}, "
        "{{lastName}} en {{company}} waar relevant - laat ze letterlijk staan, "
        "vul ze niet in. Subject: kort en persoonlijk. Body: max ~80 woorden, "
        "mag eenvoudige <br><br> gebruiken voor alinea's, geen aanhef/afsluiting "
        "met naam (dat voegt de verzender zelf toe). "
        "Geef het antwoord ALLEEN als geldige JSON: een array van objecten met "
        'de sleutels "offer_name", "subject_template" en "body_template". Geen '
        "uitleg, geen markdown-codeblok, alleen de JSON-array."
    )
    context = _profile_context(value_proposition, usps)
    if persona:
        context += f"\n\nSchrijf specifiek voor deze buyer persona: {persona.get('name')}"
        if persona.get("description"):
            context += f" ({persona['description']})"
    context += f"\n\nGenereer precies {count} verschillende variant(en)."

    message = client.messages.create(
        model=_MODEL,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": context}],
    )
    raw = _text_of(message)
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    variants = json.loads(raw)
    if not isinstance(variants, list) or not variants:
        raise ValueError("Claude gaf geen bruikbare variant-lijst terug")
    cleaned = []
    for v in variants[:count]:
        if not all(k in v for k in ("offer_name", "subject_template", "body_template")):
            continue
        cleaned.append({
            "offer_name": v["offer_name"],
            "subject_template": v["subject_template"],
            "body_template": v["body_template"],
        })
    if not cleaned:
        raise ValueError("Claude gaf geen variant terug met de verwachte velden")
    return cleaned
