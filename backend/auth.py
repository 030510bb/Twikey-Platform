"""
FastAPI auth dependencies: resolves the logged-in account from a Bearer
token, and gates the account-bootstrap endpoint with a shared admin secret.

Session tokens are opaque random strings looked up in the `sessions` table
(see database.py) - not JWTs. Simpler to reason about and to revoke (a
logout just deletes the row), at the cost of a DB lookup per request, which
is fine at this scale.
"""

import os

from fastapi import Header, HTTPException

import database


def get_current_account(authorization: str = Header(default=None)) -> dict:
    """Dependency: require a valid 'Authorization: Bearer <token>' header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Niet ingelogd. Voeg een Authorization: Bearer <token> header toe.")
    token = authorization.split(" ", 1)[1].strip()
    account = database.get_account_by_token(token)
    if not account:
        raise HTTPException(status_code=401, detail="Sessie ongeldig of verlopen. Log opnieuw in.")
    return account


def get_current_admin(authorization: str = Header(default=None)) -> dict:
    """Dependency: require a valid admin (support/superadmin) Bearer token -
    completely separate session space from get_current_account above. An
    admin token issued by /api/superadmin/login can never satisfy a
    customer-scoped 🔒 endpoint, and a customer session token can never
    satisfy a /api/superadmin/* endpoint, because they're looked up in two
    different tables (admin_sessions vs sessions)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Niet ingelogd als beheerder. Voeg een Authorization: Bearer <token> header toe.")
    token = authorization.split(" ", 1)[1].strip()
    admin = database.get_admin_by_token(token)
    if not admin:
        raise HTTPException(status_code=401, detail="Beheerderssessie ongeldig of verlopen. Log opnieuw in.")
    return admin


def require_admin_secret(x_admin_secret: str = Header(default=None)) -> None:
    """Dependency: require the X-Admin-Secret header to match the ADMIN_SECRET env var.

    Used only by the account-bootstrap endpoint (POST /api/admin/accounts) so
    that creating a new customer account isn't a fully public, unauthenticated
    action - without building a whole admin UI for what is, for now, a
    handful of manually-onboarded customers.
    """
    # Read at call time, not at import time: an env var read as a module-level
    # constant is only ever as fresh as whatever ran before this module was
    # first imported (see the load_dotenv() ordering note in app.py) - reading
    # it lazily here means that ordering can never silently break this check.
    admin_secret = os.environ.get("ADMIN_SECRET")
    if not admin_secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET is niet ingesteld op de server. Zet deze environment variable voordat je accounts kunt aanmaken.",
        )
    if not x_admin_secret or x_admin_secret != admin_secret:
        raise HTTPException(status_code=403, detail="Ongeldige of ontbrekende X-Admin-Secret header.")
