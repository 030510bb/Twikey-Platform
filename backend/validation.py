"""
Rule-based outreach message validation for the "Validation" tab.

This replaces the hardcoded "96/100, all checks PASS" example in the
mockup with checks that actually run against whatever text you type in.

Being upfront about what this is: these are deterministic, rule-based
heuristics (word lists, length counts, regex patterns) - not a machine-
learning grammar checker. That is a reasonable, honest scope for a
same-session build with no external paid API; "Grammar" below is a light
heuristic (repeated words, double spaces, missing capitalisation), not a
full grammar check. If you later want true grammar checking, that would
mean wiring in an external service (e.g. LanguageTool) and its own API key.
"""

import re

SPAM_WORDS = [
    "free money", "act now", "click here", "buy now", "limited time",
    "guarantee", "risk-free", "no obligation", "urgent", "congratulations",
    "winner", "cash bonus", "100% free", "cheap", "discount now",
]

# Simple heuristics for "unprofessional" tone: shouting caps, excess punctuation.
SHOUTING_RE = re.compile(r"\b[A-Z]{4,}\b")
EXCESS_PUNCT_RE = re.compile(r"[!?]{2,}")

# Patterns that should never appear in an outreach message (compliance).
COMPLIANCE_PATTERNS = {
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    "creditcardnummer": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "BSN/SSN-achtig nummer": re.compile(r"\b\d{9}\b"),
}

PLATFORM_LIMITS = {
    "email": 5000,       # generous ceiling, mostly a sanity check
    "linkedin": 300,     # LinkedIn connection-note character limit
}


def _check_spam_words(text: str):
    lower = text.lower()
    hits = [w for w in SPAM_WORDS if w in lower]
    passed = len(hits) == 0
    detail = "Geen trigger-woorden gevonden" if passed else f"Gevonden: {', '.join(hits)}"
    return {"name": "Spam Words", "passed": passed, "detail": detail}


def _check_length(text: str):
    word_count = len(text.split())
    passed = 15 <= word_count <= 250
    detail = f"{word_count} woorden (optimaal: 15-250)"
    return {"name": "Length", "passed": passed, "detail": detail}


def _check_personalization(text: str):
    tokens = re.findall(r"\{\{(\w+)\}\}", text)
    passed = len(tokens) > 0
    detail = f"Placeholders gevonden: {', '.join('{{' + t + '}}' for t in tokens)}" if passed \
        else "Geen {{firstName}}/{{company}}-achtige placeholder gevonden"
    return {"name": "Personalization", "passed": passed, "detail": detail}


def _check_grammar_heuristic(text: str):
    issues = []
    if "  " in text:
        issues.append("dubbele spaties")
    words = text.split()
    for i in range(1, len(words)):
        if words[i].lower() == words[i - 1].lower() and words[i].isalpha():
            issues.append(f"herhaald woord: '{words[i]}'")
            break
    stripped = text.strip()
    if stripped and stripped[0].isalpha() and not stripped[0].isupper():
        issues.append("bericht begint niet met een hoofdletter")
    passed = len(issues) == 0
    detail = "Geen problemen gevonden (heuristische check)" if passed else "; ".join(issues)
    return {"name": "Grammar (heuristisch)", "passed": passed, "detail": detail}


def _check_tone(text: str):
    shouting = SHOUTING_RE.findall(text)
    excess_punct = EXCESS_PUNCT_RE.findall(text)
    passed = not shouting and not excess_punct
    problems = []
    if shouting:
        problems.append(f"hoofdletters: {', '.join(set(shouting))}")
    if excess_punct:
        problems.append("herhaalde !/? tekens")
    detail = "Conversationele toon" if passed else "; ".join(problems)
    return {"name": "Professional Tone", "passed": passed, "detail": detail}


def _check_compliance(text: str):
    hits = [label for label, pattern in COMPLIANCE_PATTERNS.items() if pattern.search(text)]
    passed = len(hits) == 0
    detail = "Geen gevoelige data gevonden" if passed else f"Mogelijk aanwezig: {', '.join(hits)}"
    return {"name": "Compliance", "passed": passed, "detail": detail}


def _check_platform_rules(text: str, platform: str):
    limit = PLATFORM_LIMITS.get(platform, PLATFORM_LIMITS["email"])
    passed = len(text) <= limit
    detail = f"{len(text)}/{limit} tekens toegestaan voor {platform}"
    return {"name": "Platform Rules", "passed": passed, "detail": detail}


def validate_message(text: str, platform: str = "linkedin") -> dict:
    """
    Run all checks against `text` and return {checks: [...], score, passed}.
    `platform` affects only the character-limit check ("email" or "linkedin").
    """
    checks = [
        _check_spam_words(text),
        _check_length(text),
        _check_personalization(text),
        _check_grammar_heuristic(text),
        _check_tone(text),
        _check_compliance(text),
        _check_platform_rules(text, platform),
    ]
    passed_count = sum(1 for c in checks if c["passed"])
    score = round((passed_count / len(checks)) * 100)
    return {
        "checks": checks,
        "score": score,
        "passed": passed_count == len(checks),
    }
