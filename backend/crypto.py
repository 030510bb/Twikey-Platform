"""
Small symmetric-encryption helper for secrets we store at rest - currently
only customer SMTP passwords (see the smtp_settings table in database.py).

Uses Fernet (AES-128-CBC + HMAC, from the `cryptography` package), keyed by
the ENCRYPTION_KEY environment variable. Fernet also authenticates the
ciphertext, so a tampered or corrupted value fails to decrypt loudly instead
of silently returning garbage.

Generate a key once with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

and set it as ENCRYPTION_KEY (Render's Blueprint auto-generates one for you -
see render.yaml). Never commit the real value to git, and never change it on
a server that already has saved SMTP settings - existing ciphertext can only
be decrypted with the key it was encrypted under, so rotating the key means
every account has to re-enter their SMTP password.
"""

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is niet ingesteld op de server. Genereer er een met: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
            "en zet die als environment variable (zie README.md)."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
