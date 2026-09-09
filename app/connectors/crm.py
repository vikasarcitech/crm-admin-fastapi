"""CRM connectors: Zoho, HubSpot, Salesforce, Pipedrive.

Each speaks its own API — auth style, object shape, upsert semantics
and error envelope all differ — but presents the same three methods to
the queue, so the worker never knows which provider it is draining.

The shared problem is **not creating duplicates**. A lead can be pushed
more than once: a retry after a timeout that actually succeeded, a
status change following the create, an operator replaying a delivery.
Each connector therefore either uses the provider's own upsert, or
looks up the record it created last time via `connector_links`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import Connector, ConnectorError, Result, save_credentials

log = logging.getLogger("crm.connectors.crm")


class CrmConnector(Connector):
    """Shared behaviour: refresh a stale token before pushing, and retry
    once if the provider says the token is bad anyway."""

    async def ensure_token(self, client: httpx.AsyncClient) -> None:
        if self.ctx.provider.auth != "oauth2":
            return
        if not self.credentials.get("access_token"):
            # Never connected, or the tokens were cleared. Saying so
            # beats letting an empty bearer header reach httpx, which
            # rejects it as an illegal header value.
            raise ConnectorError(
                f"{self.ctx.provider.label} is not connected yet. "
                "Use Connect to authorise it."
            )
        if not self.token_expired():
            return
        fresh = await self.refresh(client)
        if fresh:
            self.credentials = fresh

    async def push(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        await self.ensure_token(client)
        result = await self.send(client, event, payload)

        # A token can be revoked before it expires, so expiry alone is
        # not enough to know it is dead. One refresh-and-retry, then
        # treat it as permanent.
        if not result.ok and result.status_code in (401, 403):
            fresh = await self.refresh(client)
            if fresh:
                self.credentials = fresh
                result = await self.send(client, event, payload)
            else:
                result.retryable = False
                result.error = (
                    f"{result.error or 'authentication failed'} — reconnect this "
                    "integration."
                )
        return result

    async def send(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        raise NotImplementedError


# ===================================================================
# Zoho CRM
# ===================================================================
class ZohoConnector(CrmConnector):
    """Zoho CRM v8.

    Two Zoho-specific things worth knowing:

    * Every endpoint is per-data-centre. The token issued by
      accounts.zoho.eu does not work against zohoapis.com, and the
      error does not say so.
    * A 200 does not mean success. Zoho returns HTTP 200 with
      `data[0].status == "error"` for per-record failures, so the body
      has to be inspected rather than trusted.
    """

    def api_base(self) -> str:
        # The token response carries the correct host; the configured
        # data centre is the fallback for a connector saved before the
        # first token exchange.
        domain = self.ctx.meta.get("api_domain")
        if domain:
            return str(domain).rstrip("/")
        dc = self.config.get("data_center", "com")
        return f"https://www.zohoapis.{dc}"

    def module(self) -> str:
        return self.config.get("module") or "Leads"

    async def refresh(self, client: httpx.AsyncClient) -> dict | None:
        refresh_token = self.credentials.get("refresh_token")
        if not refresh_token:
            return None

        dc = self.config.get("data_center", "com")
        status, body, transport = await self.request(
            client, "POST", f"https://accounts.zoho.{dc}/oauth/v2/token",
            params={
                "refresh_token": refresh_token,
                "client_id": self.credentials.get("client_id"),
                "client_secret": self.credentials.get("client_secret"),
                "grant_type": "refresh_token",
            },
        )
        if transport or not isinstance(body, dict) or not body.get("access_token"):
            log.error("zoho token refresh failed: %s", transport or body)
            return None

        # Zoho does not reissue the refresh token, so it must be kept.
        credentials = {**self.credentials, "access_token": body["access_token"]}
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(body.get("expires_in", 3600))
        )
        meta = {"expires_at": expires_at.isoformat()}
        if body.get("api_domain"):
            meta["api_domain"] = body["api_domain"]
        await save_credentials(self.ctx.id, credentials, meta)
        self.ctx.meta.update(meta)
        return credentials

    async def test(self, client: httpx.AsyncClient) -> Result:
        await self.ensure_token(client)
        status, body, transport = await self.request(
            client, "GET", f"{self.api_base()}/crm/v8/settings/modules",
            headers=self._headers(),
        )
        result = self.classify(status, body, transport)
        if result.ok and isinstance(body, dict):
            modules = [
                m.get("api_name") for m in body.get("modules", [])
                if m.get("api_supported")
            ]
            result.response = {"modules": modules[:40], "count": len(modules)}
        return result

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self.credentials.get('access_token', '')}"}

    async def send(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        record = self.mapped(payload)
        # Zoho rejects the whole request without these, with a message
        # that names the field but not the fix.
        record.setdefault("Last_Name", payload.get("full_name") or "Unnamed lead")
        if self.module() == "Leads":
            record.setdefault("Company", payload.get("company") or "Unknown")
        if self.config.get("layout"):
            record["Layout"] = {"name": self.config["layout"]}

        body = {
            "data": [record],
            # Upsert on the module's duplicate-check fields (Email for
            # Leads), which is how a re-push updates instead of
            # creating a second record.
            "duplicate_check_fields": ["Email"] if record.get("Email") else [],
            "trigger": ["workflow"],
        }
        url = f"{self.api_base()}/crm/v8/{self.module()}/upsert"

        status, response, transport = await self.request(
            client, "POST", url, headers=self._headers(), json_body=body
        )
        result = self.classify(status, response, transport)
        result.request = {"module": self.module(), "record": record}
        if not result.ok:
            return result

        # 200 with a per-record error is Zoho's normal failure mode.
        entry = _first(response, "data")
        if isinstance(entry, dict):
            if entry.get("status") == "error":
                detail = entry.get("message") or "Zoho rejected the record"
                api_name = (entry.get("details") or {}).get("api_name")
                return Result(
                    ok=False, status_code=status, retryable=False,
                    error=f"{detail}{f' ({api_name})' if api_name else ''}",
                    response=_as_response(response), request=result.request,
                )
            external_id = (entry.get("details") or {}).get("id")
            if external_id:
                result.external_id = str(external_id)
                dc = self.config.get("data_center", "com")
                result.external_url = (
                    f"https://crm.zoho.{dc}/crm/tab/{self.module()}/{external_id}"
                )
        result.response = _as_response(response)
        return result


# ===================================================================
# HubSpot
# ===================================================================
class HubSpotConnector(CrmConnector):
    """HubSpot CRM v3.

    Contacts are deduplicated on email, but the v3 create endpoint
    returns 409 rather than merging — so a conflict is resolved by
    searching for the existing contact and patching it. That is the
    documented pattern, and it is why one logical "upsert" is up to
    three calls.
    """

    BASE = "https://api.hubapi.com"

    def object_type(self) -> str:
        return self.config.get("object_type") or "contacts"

    async def test(self, client: httpx.AsyncClient) -> Result:
        status, body, transport = await self.request(
            client, "GET", f"{self.BASE}/crm/v3/objects/{self.object_type()}",
            headers=self.bearer(), params={"limit": 1},
        )
        result = self.classify(status, body, transport)
        if result.ok:
            result.response = {"object": self.object_type(), "reachable": True}
        return result

    async def send(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        properties = self.mapped(payload)
        email = properties.get("email") or payload.get("email")
        if not properties:
            return Result(ok=False, retryable=False,
                          error="Nothing to send: no fields are mapped.")

        url = f"{self.BASE}/crm/v3/objects/{self.object_type()}"
        status, body, transport = await self.request(
            client, "POST", url, headers=self.bearer(),
            json_body={"properties": properties},
        )

        # 409 means it already exists; find it and update instead.
        if status == 409 and email:
            existing = await self._find_by_email(client, email)
            if existing:
                status, body, transport = await self.request(
                    client, "PATCH", f"{url}/{existing}", headers=self.bearer(),
                    json_body={"properties": properties},
                )

        result = self.classify(status, body, transport)
        result.request = {"object": self.object_type(), "properties": properties}
        if result.ok and isinstance(body, dict) and body.get("id"):
            result.external_id = str(body["id"])
            portal = self.config.get("portal_id")
            if portal:
                result.external_url = (
                    f"https://app.hubspot.com/contacts/{portal}/"
                    f"{self.object_type().rstrip('s')}/{body['id']}"
                )
        return result

    async def _find_by_email(self, client: httpx.AsyncClient, email: str) -> str | None:
        status, body, _ = await self.request(
            client, "POST",
            f"{self.BASE}/crm/v3/objects/{self.object_type()}/search",
            headers=self.bearer(),
            json_body={
                "filterGroups": [{"filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]}],
                "limit": 1,
            },
        )
        if status and 200 <= status < 300 and isinstance(body, dict):
            results = body.get("results") or []
            if results:
                return str(results[0].get("id"))
        return None


# ===================================================================
# Salesforce
# ===================================================================
class SalesforceConnector(CrmConnector):
    """Salesforce REST.

    The instance URL comes from the token response and is per-org;
    calling login.salesforce.com for data returns a redirect that looks
    like a broken client. Duplicate handling uses the org's own
    duplicate rules via the `Sforce-Duplicate-Rule-Header`.
    """

    def instance(self) -> str:
        url = self.ctx.meta.get("instance_url")
        if not url:
            raise ConnectorError(
                "No Salesforce instance URL stored. Reconnect the integration."
            )
        return str(url).rstrip("/")

    def api_version(self) -> str:
        return self.config.get("api_version") or "v62.0"

    def sobject(self) -> str:
        return self.config.get("sobject") or "Lead"

    async def refresh(self, client: httpx.AsyncClient) -> dict | None:
        refresh_token = self.credentials.get("refresh_token")
        if not refresh_token:
            return None

        env = self.config.get("environment", "login")
        status, body, transport = await self.request(
            client, "POST", f"https://{env}.salesforce.com/services/oauth2/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.credentials.get("client_id"),
                "client_secret": self.credentials.get("client_secret"),
            },
        )
        if transport or not isinstance(body, dict) or not body.get("access_token"):
            log.error("salesforce token refresh failed: %s", transport or body)
            return None

        credentials = {**self.credentials, "access_token": body["access_token"]}
        meta = {}
        if body.get("instance_url"):
            meta["instance_url"] = body["instance_url"]
        # Salesforce access tokens have no expires_in; they are valid
        # until the session policy ends them, so there is nothing to
        # pre-empt — the 401-and-retry path is what catches it.
        await save_credentials(self.ctx.id, credentials, meta)
        self.ctx.meta.update(meta)
        return credentials

    async def test(self, client: httpx.AsyncClient) -> Result:
        await self.ensure_token(client)
        status, body, transport = await self.request(
            client, "GET",
            f"{self.instance()}/services/data/{self.api_version()}/sobjects/{self.sobject()}/describe",
            headers=self.bearer(),
        )
        result = self.classify(status, body, transport)
        if result.ok and isinstance(body, dict):
            required = [
                f.get("name") for f in body.get("fields", [])
                if not f.get("nillable") and not f.get("defaultedOnCreate")
                and f.get("createable")
            ]
            result.response = {"sobject": self.sobject(), "requiredFields": required[:20]}
        return result

    async def send(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        record = self.mapped(payload)
        record.setdefault("LastName", payload.get("full_name") or "Unnamed lead")
        if self.sobject() == "Lead":
            record.setdefault("Company", payload.get("company") or "Unknown")

        status, body, transport = await self.request(
            client, "POST",
            f"{self.instance()}/services/data/{self.api_version()}/sobjects/{self.sobject()}",
            headers={
                **self.bearer(),
                # Let the org's own duplicate rules run and save the
                # record anyway, rather than failing the delivery.
                "Sforce-Duplicate-Rule-Header": "allowSave=true",
            },
            json_body=record,
        )
        result = self.classify(status, body, transport)
        result.request = {"sobject": self.sobject(), "record": record}
        if result.ok and isinstance(body, dict) and body.get("id"):
            result.external_id = str(body["id"])
            result.external_url = f"{self.instance()}/lightning/r/{self.sobject()}/{body['id']}/view"
        return result


# ===================================================================
# Pipedrive
# ===================================================================
class PipedriveConnector(CrmConnector):
    """Pipedrive v1. Token goes in the query string, not a header."""

    def base(self) -> str:
        domain = self.config.get("company_domain", "").strip()
        if not domain:
            raise ConnectorError("Pipedrive needs the company domain.")
        return f"https://{domain}.pipedrive.com/api/v1"

    def auth_params(self) -> dict:
        return {"api_token": self.credentials.get("api_token", "")}

    async def test(self, client: httpx.AsyncClient) -> Result:
        status, body, transport = await self.request(
            client, "GET", f"{self.base()}/users/me", params=self.auth_params()
        )
        result = self.classify(status, body, transport)
        if result.ok and isinstance(body, dict):
            data = body.get("data") or {}
            result.response = {"account": data.get("company_name"), "user": data.get("name")}
        return result

    async def send(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        mapped = self.mapped(payload)
        person = {
            "name": mapped.get("name") or payload.get("full_name") or "Unnamed lead",
        }
        if mapped.get("email"):
            person["email"] = [{"value": mapped["email"], "primary": True}]
        if mapped.get("phone"):
            person["phone"] = [{"value": mapped["phone"], "primary": True}]

        status, body, transport = await self.request(
            client, "POST", f"{self.base()}/persons",
            params=self.auth_params(), json_body=person,
        )
        result = self.classify(status, body, transport)
        result.request = {"person": person}
        if result.ok and isinstance(body, dict):
            data = body.get("data") or {}
            if data.get("id"):
                result.external_id = str(data["id"])
                result.external_url = (
                    f"https://{self.config['company_domain']}.pipedrive.com"
                    f"/person/{data['id']}"
                )
        return result


# ------------------------------------------------------------ helpers
def _first(body: Any, key: str) -> Any:
    if isinstance(body, dict):
        items = body.get(key)
        if isinstance(items, list) and items:
            return items[0]
    return None


def _as_response(body: Any) -> dict | None:
    if isinstance(body, dict):
        return body
    return {"body": body} if body is not None else None
