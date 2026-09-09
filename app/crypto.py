"""Encryption for integration credentials at rest.

Connector credentials are OAuth refresh tokens and provider API keys —
long-lived bearer secrets for someone else's CRM. A database dump that
hands an attacker those is materially worse than one that hands over
lead data, because it reaches systems this platform does not own.

Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`) rather than
something hand-rolled: authenticated, versioned, and it refuses to
decrypt a tampered token instead of returning garbage.

**Key rotation** is why CREDENTIALS_KEY takes a list. The first key
encrypts; every key can decrypt. To rotate: prepend a new key, deploy,
call `rotate_all()` to re-encrypt, then drop the old key.

If no key is configured the module refuses to encrypt rather than
storing plaintext. A connector that cannot be saved is a visible
problem; a credential quietly stored in the clear is not.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import settings

log = logging.getLogger("crm.crypto")

_cipher: MultiFernet | None = None
_loaded = False


class CredentialError(RuntimeError):
    """Encryption is unavailable or a stored value cannot be read."""


def generate_key() -> str:
    """A fresh key, for `python -c 'from app.crypto import generate_key; print(generate_key())'`."""
    return Fernet.generate_key().decode()


def _load() -> MultiFernet | None:
    global _cipher, _loaded
    if _loaded:
        return _cipher

    _loaded = True
    keys = [k.strip() for k in (settings.credentials_key or "").split(",") if k.strip()]
    if not keys:
        log.warning(
            "CREDENTIALS_KEY is not set — connector credentials cannot be stored. "
            "Generate one with: python -c "
            "\"from app.crypto import generate_key; print(generate_key())\""
        )
        return None

    ciphers = []
    for index, key in enumerate(keys):
        try:
            ciphers.append(Fernet(key.encode()))
        except (ValueError, TypeError) as exc:
            # Naming the position, never the value.
            raise CredentialError(
                f"CREDENTIALS_KEY entry {index + 1} is not a valid Fernet key. "
                "It must be 32 url-safe base64-encoded bytes."
            ) from exc

    _cipher = MultiFernet(ciphers)
    return _cipher


def available() -> bool:
    """Whether credentials can be stored at all. Surfaced in the UI so
    the reason a connector cannot be saved is visible before you try."""
    try:
        return _load() is not None
    except CredentialError:
        return False


def encrypt(value: dict[str, Any] | None) -> str | None:
    """Encrypt a credential bundle. None and {} store as NULL."""
    if not value:
        return None
    cipher = _load()
    if cipher is None:
        raise CredentialError(
            "Cannot store credentials: CREDENTIALS_KEY is not configured on this "
            "install. Set it and restart before connecting an integration."
        )
    return cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()


def decrypt(token: str | None) -> dict[str, Any]:
    """Decrypt a credential bundle. Missing or unreadable returns {}.

    Unreadable is logged and swallowed rather than raised: the usual
    cause is a key that was rotated out, and the right outcome is the
    connector showing as needing reconnection — not every request that
    touches it returning 500.
    """
    if not token:
        return {}
    cipher = _load()
    if cipher is None:
        return {}
    try:
        return json.loads(cipher.decrypt(token.encode()).decode())
    except InvalidToken:
        log.error(
            "stored credential could not be decrypted — the key that encrypted it "
            "is no longer in CREDENTIALS_KEY"
        )
        return {}
    except (ValueError, TypeError) as exc:
        log.error("stored credential is malformed: %s", exc)
        return {}


def rotate(token: str | None) -> str | None:
    """Re-encrypt under the newest key, keeping the plaintext.

    MultiFernet.rotate does this without the value passing through
    application memory as a dict.
    """
    if not token:
        return None
    cipher = _load()
    if cipher is None:
        raise CredentialError("CREDENTIALS_KEY is not configured.")
    try:
        return cipher.rotate(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialError(
            "That credential was encrypted with a key that is no longer configured."
        ) from exc


async def rotate_all() -> dict:
    """Re-encrypt every stored credential under the newest key.

    Run after prepending a new key and before dropping the old one.
    Reports what it could not read rather than failing the whole pass,
    so one orphaned row does not block the rotation.
    """
    from . import db  # noqa: PLC0415 — avoids a cycle at import

    rows = await db.fetch(
        "SELECT id, credentials FROM connectors WHERE credentials IS NOT NULL"
    )
    rotated = failed = 0
    for row in rows:
        try:
            fresh = rotate(row["credentials"])
        except CredentialError:
            failed += 1
            await db.execute(
                """UPDATE connectors
                      SET status = 'expired',
                          last_error = 'Credential could not be decrypted after key rotation.',
                          last_error_at = now()
                    WHERE id = $1""",
                row["id"],
            )
            continue
        await db.execute(
            "UPDATE connectors SET credentials = $2 WHERE id = $1", row["id"], fresh
        )
        rotated += 1

    log.info("credential rotation: %d re-encrypted, %d unreadable", rotated, failed)
    return {"rotated": rotated, "unreadable": failed}


def redact(value: str | None, keep: int = 4) -> str:
    """`sk_live_abc…wxyz` for showing a stored key without revealing it."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= keep * 2:
        return "•" * len(text)
    return f"{text[:keep]}…{text[-keep:]}"
