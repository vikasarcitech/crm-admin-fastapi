"""Integrations (connectors).

CRUD over `connectors`, the OAuth handshake, a test button, a mapping
preview and the delivery log.

Credentials never leave this process. Every response goes through
`_public()`, which drops the encrypted blob and replaces it with a list
of which credential fields are set — enough for the UI to say "API key
configured" without ever being able to read it back.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import crypto, db, events, mail
from ..connectors import dispatch, oauth
from ..connectors.base import CONNECTOR_COLUMNS
from ..connectors.registry import CONFIGURABLE, PROVIDERS, catalogue, describe, get
from ..permissions import require_perm
from ..schemas import (
    ConnectorCreate,
    ConnectorReplay,
    ConnectorTestPayload,
    ConnectorUpdate,
    collapse,
)
from ..security import CurrentUser, client_ip, tenant_db

log = logging.getLogger("crm.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
public_router = APIRouter(tags=["integrations-oauth"])

SAMPLE_LEAD = {
    "id": 0,
    "full_name": "Priya Nair",
    "email": "priya@northwind.example",
    "phone": "+971 50 118 2244",
    "company": "Northwind FZ-LLC",
    "message": "Need a corporate site rebuild before Q4.",
    "status": "new",
    "source_page": "/pricing",
    "utm": {"source": "google", "medium": "cpc", "campaign": "uae-brand"},
    "form": "Contact form",
}


# ===================================================================
# Shaping
# ===================================================================
def _public(row: dict) -> dict:
    """A connector, safe to send to a browser.

    The encrypted blob is dropped and replaced with which fields are
    present. There is no endpoint anywhere that returns a credential.
    """
    provider = PROVIDERS.get(row["provider"])
    stored = crypto.decrypt(row.get("credentials"))
    meta = row.get("credentials_meta") or {}

    out = {
        key: value for key, value in row.items()
        if key not in {"credentials", "credentials_meta"}
    }
    out["providerLabel"] = provider.label if provider else row["provider"]
    out["auth"] = provider.auth if provider else "api_key"
    out["docsUrl"] = provider.docs_url if provider else None
    out["credentialsSet"] = sorted(k for k, v in stored.items() if v)
    out["hasRefreshToken"] = bool(stored.get("refresh_token"))
    out["connectedAt"] = meta.get("connected_at")
    out["tokenExpiresAt"] = meta.get("expires_at")
    out["scopes"] = meta.get("scopes")
    out["warning"] = meta.get("warning")
    out["usingDefaultMapping"] = not (row.get("field_mapping") or {})
    return out


async def _load(scoped: db.TenantDB, connector_id: int) -> dict:
    row = await scoped.fetch_one(
        f"SELECT {CONNECTOR_COLUMNS} FROM connectors WHERE tenant_id = $1 AND id = $2",
        connector_id,
    )
    if not row:
        raise HTTPException(404, "That integration no longer exists.")
    return row


def _validate_against(provider_key: str, config: dict | None, credentials: dict | None) -> tuple[dict, dict]:
    """Keep only keys the provider declares, and require what it marks
    required. An unknown key is refused rather than dropped, because
    silently ignoring a setting someone typed is how a connector is
    "configured" and does nothing."""
    provider = get(provider_key)
    config = config or {}
    credentials = credentials or {}

    known_config = {f.name: f for f in provider.config_fields}
    known_creds = {f.name: f for f in provider.credential_fields}

    unknown = (set(config) - set(known_config)) | (set(credentials) - set(known_creds))
    if unknown:
        allowed = sorted(set(known_config) | set(known_creds))
        raise HTTPException(
            400,
            f"Unknown setting(s) for {provider.label}: {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(allowed)}",
        )

    clean_config: dict = {}
    for name, field in known_config.items():
        value = config.get(name, field.default)
        if value in (None, ""):
            continue
        text = collapse(str(value), 500)
        if field.options and text not in {v for v, _ in field.options}:
            raise HTTPException(
                400,
                f"{field.label} must be one of: "
                f"{', '.join(v for v, _ in field.options)}",
            )
        clean_config[name] = text

    clean_creds: dict = {}
    for name, field in known_creds.items():
        if name not in credentials:
            continue
        value = credentials[name]
        # An explicit empty value clears a stored secret.
        clean_creds[name] = str(value).strip() if value not in (None, "") else ""

    return clean_config, clean_creds


def _missing_required(provider_key: str, config: dict, stored_creds: dict) -> list[str]:
    provider = get(provider_key)
    missing = [f.label for f in provider.config_fields if f.required and not config.get(f.name)]
    # OAuth providers only need the client id/secret up front; the
    # tokens arrive from the handshake.
    for f in provider.credential_fields:
        if f.required and not stored_creds.get(f.name):
            missing.append(f.label)
    return missing


# ===================================================================
# Catalogue and list
# ===================================================================
@router.get("/providers")
async def providers(user: CurrentUser = Depends(require_perm("webhooks.manage"))) -> dict:
    """Every provider the platform can speak to, as data.

    The admin's setup forms are generated from this, so adding a
    provider needs no frontend change.
    """
    return {
        "providers": catalogue(),
        "kinds": ["crm", "email", "automation", "analytics", "storage"],
        # Without a key, connectors needing a secret cannot be saved —
        # better to say so before someone fills in a form.
        "credentialsConfigured": crypto.available(),
        "oauthRedirectBase": oauth.settings.oauth_redirect_base or None,
        "events": sorted(events.PLATFORM_EVENTS),
    }


@router.get("")
async def list_connectors(
    kind: str | None = Query(default=None, max_length=20),
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    rows = await scoped.fetch(
        f"""SELECT {CONNECTOR_COLUMNS},
                   (SELECT count(*) FROM connector_deliveries d
                     WHERE d.connector_id = connectors.id
                       AND d.status = 'pending')::int AS pending,
                   (SELECT count(*) FROM connector_deliveries d
                     WHERE d.connector_id = connectors.id
                       AND d.status = 'dead')::int AS failed,
                   (SELECT count(*) FROM connector_deliveries d
                     WHERE d.connector_id = connectors.id
                       AND d.status = 'delivered'
                       AND d.created_at > now() - interval '30 days')::int AS delivered_30d
              FROM connectors
             WHERE tenant_id = $1 AND ($2::text IS NULL OR kind::text = $2)
             ORDER BY kind, name""",
        kind,
    )
    return {
        "connectors": [_public(row) for row in rows],
        "credentialsConfigured": crypto.available(),
    }


@router.post("", status_code=201)
async def create_connector(
    payload: ConnectorCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    provider_key = (payload.provider or "").strip().lower()
    if provider_key not in CONFIGURABLE:
        provider = PROVIDERS.get(provider_key)
        if provider and provider.managed_elsewhere:
            raise HTTPException(
                400,
                f"{provider.label} is configured elsewhere in the admin, "
                "not as a connector.",
            )
        raise HTTPException(400, f"Unknown provider “{payload.provider}”.")

    provider = get(provider_key)
    config, credentials = _validate_against(provider_key, payload.config, payload.credentials)

    if credentials and not crypto.available():
        raise HTTPException(
            400,
            "CREDENTIALS_KEY is not configured on this install, so integration "
            "secrets cannot be stored. Set it and restart before connecting.",
        )

    scoped = db.TenantDB(user.tenant_id)
    existing = await scoped.fetch_one(
        "SELECT id FROM connectors WHERE tenant_id = $1 AND provider = $2", provider_key
    )
    if existing:
        raise HTTPException(
            400,
            f"{provider.label} is already connected on this site. Edit that "
            "integration instead of adding a second one.",
        )

    chosen = [e for e in (payload.events or []) if e in provider.events] or list(
        provider.events[:2] or provider.events
    )
    # OAuth connectors start as drafts: they are not usable until the
    # handshake completes.
    status = "draft" if provider.auth == "oauth2" else "connected"

    row = await scoped.fetch_one(
        f"""INSERT INTO connectors (tenant_id, kind, provider, name, status, config,
                                    credentials, field_mapping, events, created_by)
            VALUES ($1, $2::connector_kind, $3, $4, $5::connector_status,
                    $6::jsonb, $7, $8::jsonb, $9, $10)
            RETURNING {CONNECTOR_COLUMNS}""",
        provider.kind, provider_key,
        collapse(payload.name, 120) or provider.label,
        status, config, crypto.encrypt(credentials) if credentials else None,
        payload.field_mapping or {}, chosen, user.id,
    )
    if provider.kind == "email":
        await mail.invalidate_sender_everywhere(user.tenant_id)

    await events.log_activity(
        user.tenant_id, "connector.created", user_id=user.id,
        object_type="connector", object_id=row["id"],
        meta={"provider": provider_key}, ip=db.to_inet(client_ip(request)),
    )
    result = _public(row)
    result["missing"] = _missing_required(provider_key, config, credentials)
    return {"connector": result, "provider": describe(provider)}


@router.get("/{connector_id}")
async def connector_detail(
    connector_id: int, scoped: db.TenantDB = Depends(tenant_db)
) -> dict:
    row = await _load(scoped, connector_id)
    provider = get(row["provider"])
    deliveries = await scoped.fetch(
        """SELECT id, event, status::text AS status, attempts, response_code,
                  external_id, last_error, duration_ms, created_at, delivered_at
             FROM connector_deliveries
            WHERE tenant_id = $1 AND connector_id = $2
            ORDER BY created_at DESC LIMIT 25""",
        connector_id,
    )
    links = await scoped.fetch(
        """SELECT object_type, object_id, external_id, external_url, synced_at
             FROM connector_links
            WHERE tenant_id = $1 AND connector_id = $2
            ORDER BY synced_at DESC LIMIT 10""",
        connector_id,
    )
    return {
        "connector": _public(row),
        "provider": describe(provider),
        "deliveries": deliveries,
        "recentLinks": links,
    }


@router.patch("/{connector_id}")
async def update_connector(
    connector_id: int,
    payload: ConnectorUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await _load(scoped, connector_id)
    provider = get(row["provider"])

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    config = row["config"] or {}
    if "config" in sent:
        config, _ = _validate_against(row["provider"], payload.config, None)

    credentials_blob = row["credentials"]
    if "credentials" in sent:
        _, incoming = _validate_against(row["provider"], None, payload.credentials)
        stored = crypto.decrypt(row["credentials"])
        # Merge: a field the form did not send keeps its stored value,
        # so editing the region does not wipe the API key.
        for key, value in incoming.items():
            if value == "":
                stored.pop(key, None)
            else:
                stored[key] = value
        credentials_blob = crypto.encrypt(stored) if stored else None

    chosen = None
    if "events" in sent:
        chosen = [e for e in (payload.events or []) if e in provider.events]

    updated = await scoped.fetch_one(
        f"""UPDATE connectors
               SET name          = coalesce($3, name),
                   config        = CASE WHEN $4 THEN $5::jsonb ELSE config END,
                   credentials   = CASE WHEN $6 THEN $7 ELSE credentials END,
                   field_mapping = CASE WHEN $8 THEN $9::jsonb ELSE field_mapping END,
                   events        = coalesce($10::text[], events),
                   is_active     = coalesce($11, is_active)
             WHERE tenant_id = $1 AND id = $2
             RETURNING {CONNECTOR_COLUMNS}""",
        connector_id, collapse(payload.name, 120),
        "config" in sent, config,
        "credentials" in sent, credentials_blob,
        "field_mapping" in sent, payload.field_mapping or {},
        chosen, payload.is_active,
    )
    if provider.kind == "email":
        await mail.invalidate_sender_everywhere(user.tenant_id)

    await events.log_activity(
        user.tenant_id, "connector.updated", user_id=user.id,
        object_type="connector", object_id=connector_id,
        # Never the values, only which fields changed.
        meta={"fields": list(sent)}, ip=db.to_inet(client_ip(request)),
    )
    result = _public(updated)
    result["missing"] = _missing_required(
        row["provider"], updated["config"] or {}, crypto.decrypt(updated["credentials"])
    )
    return {"connector": result}


@router.delete("/{connector_id}")
async def delete_connector(
    connector_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await _load(scoped, connector_id)
    await scoped.execute(
        "DELETE FROM connectors WHERE tenant_id = $1 AND id = $2", connector_id
    )
    if row["kind"] == "email":
        await mail.invalidate_sender_everywhere(user.tenant_id)

    await events.log_activity(
        user.tenant_id, "connector.deleted", user_id=user.id,
        object_type="connector", object_id=connector_id,
        meta={"provider": row["provider"]}, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}


# ===================================================================
# Test, preview, replay
# ===================================================================
@router.post("/{connector_id}/test")
async def test_connector(
    connector_id: int, user: CurrentUser = Depends(require_perm("webhooks.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await _load(scoped, connector_id)

    missing = _missing_required(
        row["provider"], row["config"] or {}, crypto.decrypt(row["credentials"])
    )
    if missing:
        raise HTTPException(400, f"Still to fill in: {', '.join(missing)}")

    result = await dispatch.test_connector(row)
    return {
        "ok": result.ok,
        "statusCode": result.status_code,
        "error": result.error,
        "detail": result.response,
    }


@router.post("/{connector_id}/preview")
async def preview_mapping(
    connector_id: int,
    payload: ConnectorTestPayload,
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    """Show exactly what would be sent, without sending it."""
    scoped = db.TenantDB(user.tenant_id)
    row = await _load(scoped, connector_id)

    sample = {**SAMPLE_LEAD, **payload.model_dump(exclude_none=True)}
    return await dispatch.preview(row, sample)


@router.post("/{connector_id}/send-sample")
async def send_sample(
    connector_id: int,
    payload: ConnectorTestPayload,
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    """Queue a real delivery of a sample lead.

    Deliberately a real send: it is the only way to find out that a
    required field is missing in the destination before a real lead
    hits it. The payload is marked so the destination can spot it.
    """
    scoped = db.TenantDB(user.tenant_id)
    # Loaded to confirm it belongs to this site before queueing.
    await _load(scoped, connector_id)

    import secrets  # noqa: PLC0415

    sample = {**SAMPLE_LEAD, **payload.model_dump(exclude_none=True), "test": True}
    delivery_id = await dispatch.enqueue_manual(
        user.tenant_id, connector_id, "lead.created", sample,
        suffix=f"sample-{secrets.token_hex(4)}",
    )
    return {"ok": True, "deliveryId": delivery_id,
            "message": "Queued. The worker sends it within a few seconds."}


@router.get("/{connector_id}/deliveries")
async def deliveries(
    connector_id: int,
    status: str | None = Query(default=None, max_length=20),
    page: int = Query(default=1, ge=1, le=200),
    per_page: int = Query(default=50, ge=10, le=200),
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _load(scoped, connector_id)
    offset = (page - 1) * per_page

    rows = await scoped.fetch(
        """SELECT id, event, status::text AS status, attempts, response_code,
                  external_id, last_error, duration_ms, request, response,
                  idempotency_key, created_at, delivered_at, next_attempt_at
             FROM connector_deliveries
            WHERE tenant_id = $1 AND connector_id = $2
              AND ($3::text IS NULL OR status::text = $3)
            ORDER BY created_at DESC LIMIT $4 OFFSET $5""",
        connector_id, status, per_page, offset,
    )
    total = await scoped.fetch_one(
        """SELECT count(*)::int AS n FROM connector_deliveries
            WHERE tenant_id = $1 AND connector_id = $2
              AND ($3::text IS NULL OR status::text = $3)""",
        connector_id, status,
    )
    return {
        "deliveries": rows, "page": page, "total": total["n"],
        "pages": max(1, -(-total["n"] // per_page)),
    }


@router.post("/{connector_id}/replay")
async def replay(
    connector_id: int,
    payload: ConnectorReplay,
    user: CurrentUser = Depends(require_perm("webhooks.manage")),
) -> dict:
    """Re-queue a failed delivery.

    A dead delivery is reset in place, so the idempotency key is reused
    and the destination still cannot end up with two records. A
    *delivered* one is not replayable for the same reason.
    """
    scoped = db.TenantDB(user.tenant_id)
    await _load(scoped, connector_id)

    row = await scoped.fetch_one(
        """SELECT id, status::text AS status FROM connector_deliveries
            WHERE tenant_id = $1 AND connector_id = $2 AND id = $3""",
        connector_id, payload.delivery_id,
    )
    if not row:
        raise HTTPException(404, "That delivery no longer exists.")
    if row["status"] == "delivered":
        raise HTTPException(
            400,
            "That delivery already succeeded. Replaying it would risk a duplicate "
            "record in the destination.",
        )

    await scoped.execute(
        """UPDATE connector_deliveries
              SET status = 'pending', attempts = 0, next_attempt_at = now(),
                  last_error = NULL
            WHERE tenant_id = $1 AND id = $2""",
        payload.delivery_id,
    )
    return {"ok": True, "message": "Re-queued."}


@router.post("/deliveries/retry-failed")
async def retry_all_failed(
    user: CurrentUser = Depends(require_perm("webhooks.manage"))
) -> dict:
    """Re-queue every dead delivery for this site — after fixing a
    mapping or reconnecting, this is what clears the backlog."""
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """UPDATE connector_deliveries
              SET status = 'pending', attempts = 0, next_attempt_at = now(),
                  last_error = NULL
            WHERE tenant_id = $1 AND status = 'dead'
            RETURNING id""",
    )
    return {"ok": True, "requeued": len(rows)}


# ===================================================================
# OAuth
# ===================================================================
@router.post("/{connector_id}/oauth/start")
async def oauth_start(
    connector_id: int, user: CurrentUser = Depends(require_perm("webhooks.manage"))
) -> dict:
    """Return the provider URL to send the browser to."""
    scoped = db.TenantDB(user.tenant_id)
    row = await _load(scoped, connector_id)

    url = await oauth.start(
        tenant_id=user.tenant_id,
        connector_id=connector_id,
        provider_key=row["provider"],
        config=row["config"] or {},
        credentials=crypto.decrypt(row["credentials"]),
        user_id=user.id,
    )
    return {"authorizeUrl": url, "redirectUri": oauth.redirect_uri(row["provider"])}


@public_router.get("/api/integrations/oauth/{provider_key}/callback",
                   include_in_schema=False)
async def oauth_callback(
    provider_key: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Where the provider sends the browser back.

    Unauthenticated by necessity — the provider redirects here with no
    cookie guarantee — which is exactly why `state` is mandatory and
    single-use. Renders a small page rather than JSON because a human
    is looking at it.
    """
    if error:
        return _callback_page(
            False, f"{provider_key}: {error_description or error}"
        )
    if not code or not state:
        return _callback_page(False, "The provider did not return an authorization code.")

    try:
        result = await oauth.complete(state=state, code=code)
    except HTTPException as exc:
        return _callback_page(False, str(exc.detail))
    except Exception as exc:
        log.exception("oauth callback failed for %s", provider_key)
        return _callback_page(False, f"Unexpected error: {type(exc).__name__}")

    await events.log_activity(
        result["tenantId"], "connector.connected",
        object_type="connector", object_id=result["connectorId"],
        meta={"provider": result["provider"]}, ip=db.to_inet(client_ip(request)),
    )
    return _callback_page(True, result.get("warning"), label=result["label"])


def _callback_page(ok: bool, message: str | None, label: str = "") -> HTMLResponse:
    """A minimal page that reports the outcome and closes itself.

    Values are escaped: `message` can contain text the provider chose.
    """
    import html as _html  # noqa: PLC0415

    title = f"{label} connected" if ok else "Connection failed"
    body = _html.escape(message or ("You can close this window." if ok
                                    else "Please try again."))
    tone = "#0b6e5a" if ok else "#a6412f"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<title>{_html.escape(title)}</title>
<style>
 body{{font:15px/1.55 system-ui,sans-serif;margin:0;display:grid;place-items:center;
      height:100vh;background:#f0f1ec;color:#16211f}}
 .card{{background:#fff;border:1px solid #d9dcd4;border-radius:3px;padding:28px 32px;
        max-width:34rem;border-left:3px solid {tone}}}
 h1{{font-size:17px;margin:0 0 8px}} p{{margin:0 0 14px;color:#5e6b68}}
 button{{font:inherit;padding:7px 14px;border:1px solid #d9dcd4;border-radius:3px;
         background:#fff;cursor:pointer}}
</style>
<div class="card">
  <h1>{_html.escape(title)}</h1>
  <p>{body}</p>
  <button onclick="window.close()">Close this window</button>
</div>
<script>
  // The admin opened this in a popup and is listening for the result.
  if (window.opener) {{
    window.opener.postMessage({{ source: 'crm-oauth', ok: {str(ok).lower()} }}, '*');
    setTimeout(() => window.close(), {1200 if ok else 6000});
  }}
</script>""",
        headers={"cache-control": "no-store"},
    )
