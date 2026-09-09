"""Outbound email: a queued, provider-agnostic sender.

Emails are rows in email_outbox, drained by a worker with retries and
backoff — the same shape as the webhook queue, so a crash mid-send never
loses a notification. Providers:

  EMAIL_PROVIDER=log    dev default; prints the message to the app log
  EMAIL_PROVIDER=smtp   any SMTP relay — Amazon SES SMTP credentials in
                        production, but Brevo/SendGrid/Mailgun work the
                        same way, keeping the platform provider-agnostic.

Bodies are plain text on purpose: no template engine, nothing to escape,
and plain text clears spam filters that distrust image-heavy HTML.

**Per-site providers.** A site with an email connector configured
(app/connectors/email.py — SES, Brevo, SendGrid, Mailchimp or its own
SMTP relay) sends through that, so each client can send from their own
account and reputation. Sites without one fall back to the install-wide
EMAIL_PROVIDER settings, which is what every existing install does.

The queue, the retries and the backoff live here either way; the
connector only puts one message on the wire. That split is what makes
switching provider a settings change rather than a rewrite.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from . import db
from .config import settings
from .schemas import valid_email

log = logging.getLogger("crm.mail")

MAX_ATTEMPTS = 5
BACKOFF_MINUTES = [1, 5, 15, 60]  # then dead


# ---------------------------------------------------------------- sending
def _send_smtp(to_email: str, subject: str, body: str) -> None:
    """Blocking SMTP send; always called through asyncio.to_thread."""
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    if settings.smtp_port == 465:
        client: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
    try:
        if settings.smtp_port != 465:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)
    finally:
        client.quit()


# Which connector a site sends through, cached briefly: the outbox
# worker would otherwise re-read it for every message in a batch.
_CONNECTOR_TTL = 60.0
_connector_cache: dict[int, tuple[float, dict | None]] = {}


def invalidate_sender(tenant_id: int | None = None) -> None:
    """Called when an email connector is saved, so the next message
    uses the new settings rather than waiting out the cache."""
    if tenant_id is None:
        _connector_cache.clear()
    else:
        _connector_cache.pop(tenant_id, None)


async def _sender_for(tenant_id: int) -> dict | None:
    """This site's email connector row, or None to use the env fallback."""
    import time  # noqa: PLC0415

    hit = _connector_cache.get(tenant_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    from . import db as _db  # noqa: PLC0415

    from .connectors.base import CONNECTOR_COLUMNS  # noqa: PLC0415

    row = None
    try:
        row = await _db.fetch_one(
            f"""SELECT {CONNECTOR_COLUMNS} FROM connectors
                 WHERE tenant_id = $1 AND kind = 'email' AND is_active
                   AND status = 'connected'
                 ORDER BY updated_at DESC LIMIT 1""",
            tenant_id,
        )
    except Exception as exc:
        # A lookup failure must not stop the mail going out; the
        # install-wide provider still works.
        log.error("email connector lookup failed for tenant %s: %s", tenant_id, exc)

    _connector_cache[tenant_id] = (time.monotonic() + _CONNECTOR_TTL, row)
    return row


async def _deliver(to_email: str, subject: str, body: str, tenant_id: int | None = None) -> None:
    """Send one message. Raises on failure so the caller can retry."""
    if tenant_id:
        row = await _sender_for(tenant_id)
        if row:
            await _deliver_via_connector(row, to_email, subject, body)
            return

    if settings.email_provider == "smtp":
        if not settings.smtp_host:
            raise RuntimeError("EMAIL_PROVIDER=smtp but SMTP_HOST is not set")
        await asyncio.to_thread(_send_smtp, to_email, subject, body)
    else:
        # Dev: the message is visible in the app log instead of being sent.
        log.info("email (log provider) to=%s subject=%r\n%s", to_email, subject, body)


async def _deliver_via_connector(row: dict, to_email: str, subject: str, body: str) -> None:
    import httpx  # noqa: PLC0415

    from .connectors.base import build, mark_result  # noqa: PLC0415

    connector = build(row)
    async with httpx.AsyncClient() as client:
        result = await connector.send(client, to_email, subject, body)

    await mark_result(row["id"], result)
    if not result.ok:
        # Raised so run_email_batch applies its own backoff, and
        # flagged non-retryable so a rejected address is not retried
        # five times over six hours.
        error = PermanentSendError if not result.retryable else RuntimeError
        raise error(f"{row['provider']}: {result.error}")


class PermanentSendError(RuntimeError):
    """The provider refused in a way a retry will not fix."""


# ---------------------------------------------------------------- queueing
async def enqueue(
    tenant_id: int, recipients: list[str] | set[str], subject: str, body: str, kind: str = "generic"
) -> int:
    """One outbox row per valid, deduplicated recipient. Never raises —
    a notification failure must not break the request that triggered it."""
    queued = 0
    try:
        for raw in {r.strip().lower() for r in recipients if r}:
            address = valid_email(raw)
            if not address:
                continue
            await db.execute(
                """INSERT INTO email_outbox (tenant_id, to_email, subject, body, kind)
                   VALUES ($1, $2, $3, $4, $5)""",
                tenant_id,
                address,
                subject[:300],
                body[:20000],
                kind,
            )
            queued += 1
    except Exception as exc:
        log.error("email enqueue failed: %s", exc)
    return queued


async def run_email_batch(batch_size: int = 20) -> int:
    """Drain due outbox rows once. Returns how many were attempted."""
    try:
        due = await db.fetch(
            """SELECT id, tenant_id, to_email, subject, body, attempts
                 FROM email_outbox
                WHERE status = 'pending' AND next_attempt_at <= now()
                ORDER BY next_attempt_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED""",
            batch_size,
        )
    except Exception as exc:
        log.error("email poll failed: %s", exc)
        return 0

    for row in due:
        try:
            await _deliver(
                str(row["to_email"]), row["subject"], row["body"], row.get("tenant_id")
            )
            await db.execute(
                """UPDATE email_outbox
                      SET status = 'delivered', attempts = $2, sent_at = now(), last_error = NULL
                    WHERE id = $1""",
                row["id"],
                row["attempts"] + 1,
            )
        except Exception as exc:
            attempts = row["attempts"] + 1
            # A refused recipient or an unverified sender will be
            # refused identically five more times.
            dead = attempts >= MAX_ATTEMPTS or isinstance(exc, PermanentSendError)
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            await db.execute(
                """UPDATE email_outbox
                      SET status = $2::delivery_status, attempts = $3,
                          last_error = $4, next_attempt_at = now() + make_interval(mins => $5)
                    WHERE id = $1""",
                row["id"],
                "dead" if dead else "pending",
                attempts,
                str(exc)[:500],
                delay,
            )
            log.error("email send failed (attempt %s%s): %s",
                      attempts, ", giving up" if dead else "", exc)

    return len(due)


async def email_worker() -> None:
    """Background loop. Cancelled on shutdown by the lifespan handler."""
    while True:
        try:
            await run_email_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one bad batch must not kill the loop
            log.error("email worker error: %s", exc)
        await asyncio.sleep(settings.email_poll_seconds)


# ------------------------------------------------------------- composers
def lead_notification(lead: dict, form_name: str, origin: str) -> tuple[str, str]:
    """Subject + body for a new-lead alert."""
    name = lead.get("full_name") or "Unnamed lead"
    lines = [
        f"New lead from {form_name}:",
        "",
        f"  Name:    {name}",
        f"  Email:   {lead.get('email') or '—'}",
        f"  Phone:   {lead.get('phone') or '—'}",
        f"  Company: {lead.get('company') or '—'}",
    ]
    if lead.get("message"):
        body_lines = str(lead["message"]).splitlines()[:20]
        lines += ["", "  Message:", *[f"  {line}" for line in body_lines]]
    if lead.get("utm_source"):
        lines += ["", f"  Campaign: {lead.get('utm_source')} / {lead.get('utm_medium') or '-'}"
                      f" / {lead.get('utm_campaign') or '-'}"]
    lines += ["", f"Open it: {origin}/#/leads?open={lead['id']}"]
    return f"New lead: {name}", "\n".join(lines)


def password_reset(link: str, workspace: str) -> tuple[str, str]:
    body = (
        f"Someone asked to reset the password for your {workspace} account.\n\n"
        f"Reset it here (the link works once and expires in 30 minutes):\n\n"
        f"  {link}\n\n"
        "If this wasn't you, ignore this email — nothing changes."
    )
    return f"Reset your {workspace} password", body
