"""RFC 6238 time-based one-time passwords, for optional 2FA.

Implemented on the standard library rather than a dependency: TOTP is
an HMAC of a counter, and the whole algorithm fits in one function that
is easier to audit than a third-party package.

Replay is blocked by storing the last accepted time step
(``user_totp.last_used_step``) — without that, a code shouted over the
phone stays valid for its whole 30-second window.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
# One step either side, to tolerate clock drift between phone and server.
DRIFT_STEPS = 1

RECOVERY_CODE_COUNT = 8


def generate_secret() -> str:
    """A 160-bit base32 secret, the size RFC 4226 recommends for HMAC-SHA1."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_for_step(secret: str, step: int) -> str:
    # Re-pad: base32 decoding is strict about the '=' this library strips.
    padded = secret + "=" * (-len(secret) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed TOTP secret") from exc

    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def current_step(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // PERIOD)


def verify(secret: str, code: str, *, last_used_step: int | None = None) -> int | None:
    """Return the accepted time step, or None.

    The caller must persist the returned step so the same code cannot be
    replayed inside its window.
    """
    cleaned = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(cleaned) != DIGITS:
        return None

    now = current_step()
    for offset in range(-DRIFT_STEPS, DRIFT_STEPS + 1):
        step = now + offset
        if last_used_step is not None and step <= last_used_step:
            continue  # already spent
        if hmac.compare_digest(_code_for_step(secret, step), cleaned):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """otpauth:// URI for authenticator apps, rendered as a QR by the SPA."""
    label = quote(f"{issuer}:{account}", safe="")
    params = (
        f"secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )
    return f"otpauth://totp/{label}?{params}"


# ------------------------------------------------------------- recovery codes
def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Shown once at enrolment; only their SHA-256 is stored."""
    return [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(count)
    ]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


def match_recovery(code: str, hashes: list[str]) -> str | None:
    """The hash that matched, so the caller can consume exactly that one."""
    candidate = hash_recovery(code)
    for stored in hashes:
        if hmac.compare_digest(stored, candidate):
            return stored
    return None
