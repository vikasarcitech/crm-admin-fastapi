"""Zapier, Make and n8n.

All three receive a JSON POST at a URL they mint, so they share one
implementation. They are separate registry entries because the setup
instructions differ, and "paste your Make webhook URL" is the
difference between a working integration and a support ticket.

Two things distinguish this from the raw `webhook_endpoints` path:
field mapping (send the shape the scenario expects, not ours) and the
provider label on the delivery log.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import httpx

from .base import Connector, Result

log = logging.getLogger("crm.connectors.automation")


class AutomationConnector(Connector):
    def url(self) -> str:
        return (self.config.get("url") or "").strip()

    def _headers(self, body: str) -> dict:
        headers = {
            "content-type": "application/json",
            "user-agent": "crm-admin-connectors/1.0",
        }
        secret = self.credentials.get("secret")
        if secret:
            # Same scheme as webhook_endpoints: the signature covers the
            # timestamp too, so a captured body cannot be replayed.
            timestamp = int(time.time())
            mac = hmac.new(
                str(secret).encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
            )
            headers["x-crm-timestamp"] = str(timestamp)
            headers["x-crm-signature"] = f"sha256={mac.hexdigest()}"
        return headers

    async def test(self, client: httpx.AsyncClient) -> Result:
        """Send a marked test event.

        Zapier and Make both need a sample payload to build the
        scenario against, so a test that actually posts one is more
        useful than a HEAD request.
        """
        return await self._post(
            client,
            {
                "event": "connection.test",
                "test": True,
                "data": {
                    "full_name": "Test Lead",
                    "email": "test@example.com",
                    "company": "Example Ltd",
                    "message": "Sent by the Test button in your CRM.",
                },
            },
        )

    async def push(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        mapped = self.mapped(payload)
        body = {
            "event": event,
            "site": payload.get("site"),
            # Both shapes: `data` is the platform payload, `mapped` is
            # what the configured mapping produced. A scenario built
            # before a mapping existed keeps working.
            "data": payload,
        }
        if mapped:
            body["mapped"] = mapped
        return await self._post(client, body)

    async def _post(self, client: httpx.AsyncClient, body: dict) -> Result:
        import json  # noqa: PLC0415

        url = self.url()
        if not url:
            return Result(ok=False, retryable=False, error="No webhook URL configured.")

        raw = json.dumps(body, default=str)
        try:
            response = await client.post(
                url, content=raw, headers=self._headers(raw), timeout=20.0
            )
        except httpx.TimeoutException:
            return Result(ok=False, error="timed out after 20s", retryable=True)
        except httpx.HTTPError as exc:
            return Result(ok=False, error=str(exc)[:300], retryable=True)

        result = self.classify(response.status_code, _safe_json(response), None)
        result.request = body
        # Zapier returns a request id that is worth keeping for support.
        if result.ok and isinstance(result.response, dict):
            for key in ("id", "request_id", "requestId", "executionId"):
                if result.response.get(key):
                    result.external_id = str(result.response[key])
                    break
        return result


def _safe_json(response: httpx.Response):
    try:
        return response.json() if response.content else None
    except ValueError:
        return {"raw": response.text[:1000]}
