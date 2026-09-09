"""What every connector shares.

A provider class implements three things — `test()`, `push()` and, for
OAuth providers, `refresh()`. Everything else lives here: field
mapping, the HTTP call, how a response becomes a retry-or-give-up
decision, and how credentials are read and written back.

The `Result` distinction is the important one. A failure is either:

  retryable   the provider was unreachable, rate-limited, or returned
              5xx. Try again with backoff.
  permanent   the provider understood and refused: a required field is
              missing, a value is invalid. Retrying sends the identical
              request and gets the identical refusal, so the delivery
              is marked dead and the error surfaced to a human.

Getting that wrong in either direction is expensive: retrying a
permanent failure burns the queue for hours, and giving up on a
transient one loses a lead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import crypto, db
from .registry import Provider

log = logging.getLogger("crm.connectors")

TIMEOUT = 20.0
# Refresh a little before expiry: a token that dies mid-request costs a
# retry, and clocks are never exactly aligned.
REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(slots=True)
class Result:
    ok: bool
    status_code: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    error: str | None = None
    retryable: bool = False
    response: dict | None = None
    request: dict | None = None


@dataclass(slots=True)
class Context:
    """One connector row, decrypted and ready to use."""

    id: int
    tenant_id: int
    provider: Provider
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    field_mapping: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


class ConnectorError(Exception):
    """Configuration is wrong in a way no retry fixes."""


class Connector:
    """Base class. Subclasses implement test/push and maybe refresh."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.credentials = ctx.credentials

    # -------------------------------------------------------- interface
    async def test(self, client: httpx.AsyncClient) -> Result:
        """Verify the credentials reach the provider. Cheap and read-only."""
        raise NotImplementedError

    async def push(
        self, client: httpx.AsyncClient, event: str, payload: dict
    ) -> Result:
        """Send one event."""
        raise NotImplementedError

    async def refresh(self, client: httpx.AsyncClient) -> dict | None:
        """Renew an OAuth access token. None when the provider has no refresh."""
        return None

    # ---------------------------------------------------------- mapping
    def mapped(self, payload: dict) -> dict[str, Any]:
        """Apply the configured mapping to a flattened payload.

        Empty values are dropped rather than sent as empty strings: a
        CRM that receives `"Phone": ""` will happily store an empty
        phone number and overwrite a good one on the next update.
        """
        flat = flatten_lead(payload)
        mapping = self.ctx.field_mapping or self.ctx.provider.default_mapping
        out: dict[str, Any] = {}
        for source, target in mapping.items():
            if not target:
                continue
            value = flat.get(source)
            if value in (None, "", [], {}):
                continue
            out[target] = value
        return out

    # ------------------------------------------------------------- HTTP
    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json_body: Any = None,
        params: dict | None = None,
    ) -> tuple[int | None, Any, str | None]:
        """One call, with transport failures turned into a message.

        Returns (status, parsed body, transport error). A transport
        error means nothing reached the provider, which is always
        retryable.
        """
        try:
            response = await client.request(
                method, url,
                headers={"accept": "application/json", **(headers or {})},
                json=json_body, params=params, timeout=TIMEOUT,
            )
        except httpx.TimeoutException:
            return None, None, f"timed out after {TIMEOUT:.0f}s"
        except httpx.HTTPError as exc:
            return None, None, str(exc)[:300]

        try:
            body = response.json() if response.content else None
        except ValueError:
            body = {"raw": response.text[:2000]}
        return response.status_code, body, None

    @staticmethod
    def classify(status: int | None, body: Any, transport_error: str | None) -> Result:
        """Turn a raw response into a retry-or-not decision.

        429 and 5xx are the provider's problem and will pass; 4xx is
        ours and will not.
        """
        if transport_error:
            return Result(ok=False, error=transport_error, retryable=True)
        if status is None:
            return Result(ok=False, error="no response", retryable=True)
        if 200 <= status < 300:
            return Result(ok=True, status_code=status, response=_as_dict(body))
        if status in (408, 409, 425, 429) or status >= 500:
            return Result(
                ok=False, status_code=status, retryable=True,
                error=f"HTTP {status}: {_message(body)}", response=_as_dict(body),
            )
        # 401/403 are handled by the caller, which retries once after a
        # token refresh before treating them as permanent.
        return Result(
            ok=False, status_code=status, retryable=False,
            error=f"HTTP {status}: {_message(body)}", response=_as_dict(body),
        )

    # ---------------------------------------------------- OAuth helpers
    def token_expired(self) -> bool:
        expires = self.ctx.meta.get("expires_at")
        if not expires:
            return False
        try:
            when = datetime.fromisoformat(str(expires))
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when - REFRESH_MARGIN <= datetime.now(timezone.utc)

    def bearer(self) -> dict[str, str]:
        token = self.credentials.get("access_token") or self.credentials.get("api_key")
        return {"authorization": f"Bearer {token}"} if token else {}


def _message(body: Any) -> str:
    """Pull the human-readable part out of a provider error body.

    Every provider nests it somewhere different, and an integrator
    debugging a rejected lead needs the sentence, not the envelope.
    """
    if body is None:
        return "no body"
    if isinstance(body, str):
        return body[:300]
    if isinstance(body, list) and body:
        return _message(body[0])
    if isinstance(body, dict):
        for key in ("message", "error_description", "error", "detail", "errorMessage"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:300]
        # Zoho puts per-record failures under data[].
        data = body.get("data")
        if isinstance(data, list) and data:
            return _message(data[0])
        # Salesforce returns [{"message": …, "errorCode": …}].
        if body.get("errors"):
            return _message(body["errors"])
    return str(body)[:300]


def _as_dict(body: Any) -> dict | None:
    if body is None:
        return None
    if isinstance(body, dict):
        return body
    return {"body": body}


# ===================================================================
# Payload shaping
# ===================================================================
def flatten_lead(payload: dict) -> dict[str, Any]:
    """Platform event payload → the flat source fields mapping uses.

    Splitting `full_name` matters: most CRMs require a last name and
    have no single-name field, so a lead captured as "Priya Nair" must
    arrive as First_Name/Last_Name or Zoho and Salesforce reject it.
    """
    flat: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            flat[key] = value

    utm = payload.get("utm")
    if isinstance(utm, dict):
        for key, value in utm.items():
            flat[f"utm_{key}"] = value

    extra = payload.get("extra")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(value, (str, int, float, bool)):
                flat.setdefault(key, value)

    full_name = str(payload.get("full_name") or "").strip()
    if full_name:
        parts = full_name.split()
        flat["first_name"] = parts[0] if len(parts) > 1 else ""
        # Everything after the first token, so "Ana Maria de Souza"
        # keeps its surname intact.
        flat["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else full_name
    return flat


# ===================================================================
# Loading and persistence
# ===================================================================
# Kept as a tuple, not a formatted string: the worker joins `connectors`
# to `connector_deliveries`, where `status`, `tenant_id` and `created_at`
# all exist on both sides, so every column needs a table prefix. Building
# that with a string replace silently missed the ones after a newline and
# produced an ambiguous-column error the worker swallowed as "0 due".
CONNECTOR_FIELDS: tuple[str, ...] = (
    "id", "tenant_id", "kind::text AS kind", "provider", "name",
    "status::text AS status", "config", "credentials", "credentials_meta",
    "field_mapping", "events", "is_active", "last_ok_at", "last_error",
    "last_error_at", "created_at", "updated_at",
)


def connector_columns(prefix: str = "") -> str:
    """Column list, optionally table-qualified: connector_columns("c")."""
    if not prefix:
        return ", ".join(CONNECTOR_FIELDS)
    return ", ".join(
        # A cast/alias needs the prefix on the column, not the whole
        # expression: `c.kind::text AS kind`.
        f"{prefix}.{field}" for field in CONNECTOR_FIELDS
    )


CONNECTOR_COLUMNS = connector_columns()


def build(row: dict) -> Connector:
    """Row → live connector instance."""
    from .import_map import CONNECTOR_CLASSES  # noqa: PLC0415 — avoids a cycle

    from .registry import get  # noqa: PLC0415

    provider = get(row["provider"])
    cls = CONNECTOR_CLASSES.get(provider.key)
    if cls is None:
        raise ConnectorError(f"No implementation registered for {provider.key}.")

    ctx = Context(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider=provider,
        name=row["name"],
        config=row.get("config") or {},
        credentials=crypto.decrypt(row.get("credentials")),
        field_mapping=row.get("field_mapping") or {},
        meta=row.get("credentials_meta") or {},
    )
    return cls(ctx)


async def save_credentials(
    connector_id: int, credentials: dict, meta: dict | None = None
) -> None:
    """Persist refreshed tokens. Merges meta rather than replacing it,
    so a refresh does not lose the connected account's label."""
    await db.execute(
        """UPDATE connectors
              SET credentials = $2,
                  credentials_meta = credentials_meta || $3::jsonb,
                  status = 'connected', last_error = NULL
            WHERE id = $1""",
        connector_id, crypto.encrypt(credentials), meta or {},
    )


async def mark_result(connector_id: int, result: Result) -> None:
    """Record the outcome on the connector itself, so the list screen
    can show which integrations are healthy without opening each one."""
    if result.ok:
        await db.execute(
            """UPDATE connectors
                  SET last_ok_at = now(), last_error = NULL, last_error_at = NULL,
                      status = CASE WHEN status IN ('error', 'expired')
                                    THEN 'connected' ELSE status END
                WHERE id = $1""",
            connector_id,
        )
        return

    await db.execute(
        """UPDATE connectors
              SET last_error = $2, last_error_at = now(),
                  status = CASE WHEN $3 THEN 'expired'::connector_status
                                ELSE 'error'::connector_status END
            WHERE id = $1""",
        connector_id, (result.error or "unknown error")[:500],
        result.status_code in (401, 403),
    )
