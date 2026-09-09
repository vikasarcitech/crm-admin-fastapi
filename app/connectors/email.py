"""Email providers: SES, Brevo, SendGrid, Mailchimp Transactional, SMTP.

`mail.py` owns the queue, the retries and the backoff; these own only
"put this one message on the wire". That split is what makes the
platform provider-agnostic — switching from SES to Brevo changes which
class the outbox worker calls and nothing else.

Each exposes `send(to, subject, body, html)` in addition to the
standard `test()`, because email is the one connector kind the platform
itself calls rather than dispatching an event to.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .base import Connector, ConnectorError, Result

log = logging.getLogger("crm.connectors.email")


class EmailConnector(Connector):
    """A sender. `push` exists so the registry is uniform, but the
    outbox worker calls `send` directly."""

    def from_address(self) -> tuple[str, str | None]:
        address = (self.config.get("from_email") or "").strip()
        if not address:
            raise ConnectorError("This email connector has no From address.")
        return address, (self.config.get("from_name") or "").strip() or None

    async def send(
        self, client: httpx.AsyncClient, to: str, subject: str, body: str,
        html: str | None = None,
    ) -> Result:
        raise NotImplementedError

    async def push(self, client: httpx.AsyncClient, event: str, payload: dict) -> Result:
        return await self.send(
            client,
            payload.get("to", ""),
            payload.get("subject", ""),
            payload.get("body", ""),
            payload.get("html"),
        )

    async def test(self, client: httpx.AsyncClient) -> Result:
        """Verify credentials without sending to a third party.

        Each provider has a cheap read-only endpoint; using it means the
        Test button cannot accidentally email a real person.
        """
        raise NotImplementedError


# ===================================================================
# Amazon SES
# ===================================================================
class SesConnector(EmailConnector):
    """SES v2 through boto3.

    boto3 is blocking, so every call goes through asyncio.to_thread —
    running it inline would stall the event loop for the whole round
    trip. Credentials are optional on purpose: with none, boto3 uses
    the task's IAM role, which is the better answer on AWS because
    there is no key to rotate or leak.
    """

    def _client(self):
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:
            raise ConnectorError("SES needs boto3. Add it to requirements.txt.") from exc

        kwargs = {"region_name": self.config.get("region") or "us-east-1"}
        key = self.credentials.get("access_key_id")
        secret = self.credentials.get("secret_access_key")
        if key and secret:
            kwargs["aws_access_key_id"] = key
            kwargs["aws_secret_access_key"] = secret
        return boto3.client("sesv2", **kwargs)

    async def test(self, client: httpx.AsyncClient) -> Result:
        def _call():
            ses = self._client()
            quota = ses.get_account()
            address, _ = self.from_address()
            identity = ses.get_email_identity(EmailIdentity=address.split("@")[-1])
            return quota, identity

        try:
            quota, identity = await asyncio.to_thread(_call)
        except ConnectorError as exc:
            return Result(ok=False, retryable=False, error=str(exc))
        except Exception as exc:
            return Result(ok=False, retryable=False, error=_aws_message(exc))

        details = quota.get("SendQuota") or {}
        return Result(ok=True, status_code=200, response={
            "sendingEnabled": quota.get("SendingEnabled"),
            "max24Hour": details.get("Max24HourSend"),
            "sentLast24Hours": details.get("SentLast24Hours"),
            "domainVerified": identity.get("VerifiedForSendingStatus"),
        })

    async def send(self, client, to, subject, body, html=None) -> Result:
        address, name = self.from_address()
        source = f"{name} <{address}>" if name else address

        def _call():
            ses = self._client()
            content = {"Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            }}
            if html:
                content["Simple"]["Body"]["Html"] = {"Data": html, "Charset": "UTF-8"}
            kwargs = {
                "FromEmailAddress": source,
                "Destination": {"ToAddresses": [to]},
                "Content": content,
            }
            if self.config.get("configuration_set"):
                kwargs["ConfigurationSetName"] = self.config["configuration_set"]
            return ses.send_email(**kwargs)

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:
            # Throttling and 5xx are worth retrying; a rejected address
            # or unverified identity is not.
            name_of = type(exc).__name__
            retryable = name_of in {
                "ThrottlingException", "TooManyRequestsException",
                "SendingPausedException", "EndpointConnectionError",
            }
            return Result(ok=False, retryable=retryable, error=_aws_message(exc))

        return Result(ok=True, status_code=200,
                      external_id=response.get("MessageId"),
                      response={"messageId": response.get("MessageId")})


def _aws_message(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = error.get("Code")
        message = error.get("Message")
        if code or message:
            return f"{code}: {message}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


# ===================================================================
# Brevo
# ===================================================================
class BrevoConnector(EmailConnector):
    BASE = "https://api.brevo.com/v3"

    def _headers(self) -> dict:
        return {
            "api-key": self.credentials.get("api_key", ""),
            "content-type": "application/json",
        }

    async def test(self, client: httpx.AsyncClient) -> Result:
        status, body, transport = await self.request(
            client, "GET", f"{self.BASE}/account", headers=self._headers()
        )
        result = self.classify(status, body, transport)
        if result.ok and isinstance(body, dict):
            plan = (body.get("plan") or [{}])[0] if body.get("plan") else {}
            result.response = {
                "company": (body.get("companyName") or body.get("email")),
                "credits": plan.get("credits"),
            }
        return result

    async def send(self, client, to, subject, body, html=None) -> Result:
        address, name = self.from_address()
        payload = {
            "sender": {"email": address, **({"name": name} if name else {})},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": body,
        }
        if html:
            payload["htmlContent"] = html

        status, response, transport = await self.request(
            client, "POST", f"{self.BASE}/smtp/email",
            headers=self._headers(), json_body=payload,
        )
        result = self.classify(status, response, transport)
        if result.ok and isinstance(response, dict):
            result.external_id = response.get("messageId")
        return result


# ===================================================================
# SendGrid
# ===================================================================
class SendGridConnector(EmailConnector):
    BASE = "https://api.sendgrid.com/v3"

    async def test(self, client: httpx.AsyncClient) -> Result:
        status, body, transport = await self.request(
            client, "GET", f"{self.BASE}/scopes", headers=self.bearer()
        )
        result = self.classify(status, body, transport)
        if result.ok and isinstance(body, dict):
            scopes = body.get("scopes") or []
            result.response = {"scopes": len(scopes), "canSend": "mail.send" in scopes}
            if "mail.send" not in scopes:
                return Result(
                    ok=False, status_code=status, retryable=False,
                    error="That API key does not carry the mail.send permission.",
                )
        return result

    def bearer(self) -> dict:
        return {"authorization": f"Bearer {self.credentials.get('api_key', '')}"}

    async def send(self, client, to, subject, body, html=None) -> Result:
        address, name = self.from_address()
        content = [{"type": "text/plain", "value": body}]
        if html:
            content.append({"type": "text/html", "value": html})

        status, response, transport = await self.request(
            client, "POST", f"{self.BASE}/mail/send", headers=self.bearer(),
            json_body={
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": address, **({"name": name} if name else {})},
                "subject": subject,
                "content": content,
            },
        )
        result = self.classify(status, response, transport)
        # SendGrid answers 202 with an empty body; the id is a header,
        # which `request` does not surface, so there is nothing to keep.
        return result


# ===================================================================
# Mailchimp Transactional (Mandrill)
# ===================================================================
class MailchimpConnector(EmailConnector):
    BASE = "https://mandrillapp.com/api/1.0"

    async def test(self, client: httpx.AsyncClient) -> Result:
        status, body, transport = await self.request(
            client, "POST", f"{self.BASE}/users/ping2.json",
            json_body={"key": self.credentials.get("api_key", "")},
        )
        result = self.classify(status, body, transport)
        # Mandrill answers 200 with an error object for a bad key.
        if isinstance(body, dict) and body.get("status") == "error":
            return Result(ok=False, status_code=status, retryable=False,
                          error=body.get("message", "Mailchimp rejected the key"))
        if result.ok:
            result.response = {"ping": "ok"}
        return result

    async def send(self, client, to, subject, body, html=None) -> Result:
        address, name = self.from_address()
        message = {
            "from_email": address,
            "to": [{"email": to, "type": "to"}],
            "subject": subject,
            "text": body,
        }
        if name:
            message["from_name"] = name
        if html:
            message["html"] = html

        status, response, transport = await self.request(
            client, "POST", f"{self.BASE}/messages/send.json",
            json_body={"key": self.credentials.get("api_key", ""), "message": message},
        )
        result = self.classify(status, response, transport)
        if isinstance(response, dict) and response.get("status") == "error":
            return Result(ok=False, status_code=status, retryable=False,
                          error=response.get("message", "Mailchimp refused the message"))

        entry = response[0] if isinstance(response, list) and response else {}
        if isinstance(entry, dict):
            # 'rejected' and 'invalid' are permanent for this address.
            if entry.get("status") in {"rejected", "invalid"}:
                return Result(
                    ok=False, status_code=status, retryable=False,
                    error=f"{entry.get('status')}: {entry.get('reject_reason') or to}",
                )
            result.external_id = entry.get("_id")
        return result


# ===================================================================
# SMTP
# ===================================================================
class SmtpConnector(EmailConnector):
    """Any relay. Blocking, so it runs in a thread like SES."""

    def _send_blocking(self, to: str, subject: str, body: str, html: str | None) -> None:
        address, name = self.from_address()
        message = EmailMessage()
        message["From"] = f"{name} <{address}>" if name else address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")

        host = self.config.get("host")
        port = int(self.config.get("port") or 587)
        if not host:
            raise ConnectorError("This SMTP connector has no host.")

        if port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        try:
            if port != 465:
                client.starttls()
            username = self.credentials.get("username")
            if username:
                client.login(username, self.credentials.get("password", ""))
            client.send_message(message)
        finally:
            client.quit()

    async def test(self, client: httpx.AsyncClient) -> Result:
        def _call():
            host = self.config.get("host")
            port = int(self.config.get("port") or 587)
            if not host:
                raise ConnectorError("This SMTP connector has no host.")
            if port == 465:
                conn: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                conn = smtplib.SMTP(host, port, timeout=10)
            try:
                if port != 465:
                    conn.starttls()
                username = self.credentials.get("username")
                if username:
                    conn.login(username, self.credentials.get("password", ""))
                return conn.noop()
            finally:
                conn.quit()

        try:
            code, _ = await asyncio.to_thread(_call)
        except ConnectorError as exc:
            return Result(ok=False, retryable=False, error=str(exc))
        except smtplib.SMTPAuthenticationError as exc:
            return Result(ok=False, retryable=False,
                          error=f"Authentication refused: {exc.smtp_error.decode(errors='replace')[:200]}")
        except Exception as exc:
            return Result(ok=False, retryable=True, error=f"{type(exc).__name__}: {exc}"[:300])
        return Result(ok=True, status_code=code, response={"smtp": "reachable"})

    async def send(self, client, to, subject, body, html=None) -> Result:
        try:
            await asyncio.to_thread(self._send_blocking, to, subject, body, html)
        except ConnectorError as exc:
            return Result(ok=False, retryable=False, error=str(exc))
        except smtplib.SMTPRecipientsRefused as exc:
            return Result(ok=False, retryable=False, error=f"Recipient refused: {exc}"[:300])
        except smtplib.SMTPAuthenticationError:
            return Result(ok=False, retryable=False, error="SMTP authentication refused.")
        except Exception as exc:
            return Result(ok=False, retryable=True, error=f"{type(exc).__name__}: {exc}"[:300])
        return Result(ok=True, status_code=250)
