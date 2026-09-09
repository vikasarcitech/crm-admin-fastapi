"""OAuth 2.0 authorization-code flow for connectors.

Three things this has to get right, because getting any of them wrong
is a security problem rather than a bug:

* **State.** The callback is an unauthenticated public URL. Without a
  one-time state token bound to the tenant, anyone could complete a
  handshake and attach *their* CRM account to *someone else's* site.
  State is stored, single-use, and expires.

* **PKCE** where the provider supports it, so an intercepted
  authorization code is useless without the verifier.

* **Refresh tokens.** Zoho only issues one when explicitly asked
  (`access_type=offline&prompt=consent`), and never reissues it — so a
  refresh response that omits it must not overwrite the stored one.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from .. import crypto, db
from ..config import settings
from .registry import Provider, get

log = logging.getLogger("crm.connectors.oauth")

STATE_MINUTES = 15


def redirect_uri(provider_key: str) -> str:
    """Must match what is registered with the provider, character for
    character — a trailing slash is enough to fail the exchange."""
    base = settings.oauth_redirect_base
    if not base:
        raise HTTPException(
            400,
            "OAUTH_REDIRECT_BASE is not set on this install, so an OAuth callback "
            "URL cannot be built. Set it to this admin's public origin.",
        )
    return f"{base}/api/integrations/oauth/{provider_key}/callback"


def _data_centre(provider: Provider, config: dict) -> str:
    """The `{dc}` placeholder: Zoho's region, Salesforce's environment."""
    if provider.key == "zoho_crm":
        return config.get("data_center") or "com"
    if provider.key == "salesforce":
        return config.get("environment") or "login"
    return ""


async def start(
    *, tenant_id: int, connector_id: int, provider_key: str,
    config: dict, credentials: dict, user_id: int | None,
) -> str:
    """Mint state and return the URL to send the browser to."""
    provider = get(provider_key)
    if not provider.oauth:
        raise HTTPException(400, f"{provider.label} does not use OAuth.")

    client_id = credentials.get("client_id")
    if not client_id:
        raise HTTPException(400, "Save the client ID and secret before connecting.")

    state = secrets.token_urlsafe(32)
    verifier = None
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(provider.key),
        "scope": " ".join(provider.oauth.scopes),
        "state": state,
        **dict(provider.oauth.extra_authorize_params),
    }

    if provider.oauth.uses_pkce:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"

    await db.execute(
        """INSERT INTO connector_oauth_states
               (state, tenant_id, connector_id, provider, redirect_uri,
                code_verifier, created_by, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        state, tenant_id, connector_id, provider.key,
        params["redirect_uri"], verifier, user_id,
        datetime.now(timezone.utc) + timedelta(minutes=STATE_MINUTES),
    )

    url = provider.oauth.authorize_url.format(dc=_data_centre(provider, config))
    return f"{url}?{urlencode(params)}"


async def complete(*, state: str, code: str) -> dict:
    """Exchange the code for tokens and store them.

    The state row is consumed first, so a replayed callback cannot
    exchange a second time.
    """
    row = await db.fetch_one(
        """DELETE FROM connector_oauth_states
            WHERE state = $1 AND expires_at > now()
            RETURNING tenant_id, connector_id, provider, redirect_uri, code_verifier""",
        state,
    )
    if not row:
        raise HTTPException(
            400,
            "That authorization link has expired or was already used. "
            "Start the connection again.",
        )

    provider = get(row["provider"])
    connector = await db.fetch_one(
        """SELECT id, tenant_id, config, credentials, credentials_meta
             FROM connectors WHERE id = $1 AND tenant_id = $2""",
        row["connector_id"], row["tenant_id"],
    )
    if not connector:
        raise HTTPException(404, "That integration no longer exists.")

    credentials = crypto.decrypt(connector["credentials"])
    config = connector["config"] or {}

    params = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": credentials.get("client_id"),
        "client_secret": credentials.get("client_secret"),
        "redirect_uri": row["redirect_uri"],
    }
    if row["code_verifier"]:
        params["code_verifier"] = row["code_verifier"]

    token_url = provider.oauth.token_url.format(dc=_data_centre(provider, config))
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(token_url, data=params, timeout=20.0)
            body = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Could not reach {provider.label}: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(502, f"{provider.label} returned an unreadable response.") from exc

    if response.status_code >= 400 or not body.get("access_token"):
        detail = body.get("error_description") or body.get("error") or "no access token"
        raise HTTPException(400, f"{provider.label} refused the connection: {detail}")

    # A refresh response omits the refresh token on most providers, and
    # Zoho never reissues it — so keep what we have.
    stored = {**credentials, "access_token": body["access_token"]}
    if body.get("refresh_token"):
        stored["refresh_token"] = body["refresh_token"]

    meta: dict = {"connected_at": datetime.now(timezone.utc).isoformat()}
    if body.get("expires_in"):
        meta["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
        ).isoformat()
    if body.get("scope"):
        meta["scopes"] = body["scope"]
    if provider.oauth.instance_from_token:
        value = body.get(provider.oauth.instance_from_token)
        if value:
            meta[provider.oauth.instance_from_token] = value

    has_refresh = bool(stored.get("refresh_token"))
    if not has_refresh:
        # Worth saying plainly: without one the connector works now and
        # stops silently when the access token expires.
        meta["warning"] = (
            f"{provider.label} did not return a refresh token. The connection will "
            "stop working when the access token expires and will need reconnecting."
        )
        log.warning("no refresh token from %s for connector %s",
                    provider.key, connector["id"])

    await db.execute(
        """UPDATE connectors
              SET credentials = $2, credentials_meta = credentials_meta || $3::jsonb,
                  status = 'connected', last_error = NULL, last_error_at = NULL
            WHERE id = $1""",
        connector["id"], crypto.encrypt(stored), meta,
    )
    return {
        "connectorId": connector["id"],
        "tenantId": connector["tenant_id"],
        "provider": provider.key,
        "label": provider.label,
        "hasRefreshToken": has_refresh,
        "warning": meta.get("warning"),
    }


async def prune_states() -> int:
    rows = await db.fetch(
        "DELETE FROM connector_oauth_states WHERE expires_at < now() RETURNING state"
    )
    return len(rows)
