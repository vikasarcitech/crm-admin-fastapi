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


async def _deliver(to_email: str, subject: str, body: str) -> None:
    if settings.email_provider == "smtp":
        if not settings.smtp_host:
            raise RuntimeError("EMAIL_PROVIDER=smtp but SMTP_HOST is not set")
        await asyncio.to_thread(_send_smtp, to_email, subject, body)
    else:
        # Dev: the message is visible in the app log instead of being sent.
        log.info("email (log provider) to=%s subject=%r\n%s", to_email, subject, body)


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
            """SELECT id, to_email, subject, body, attempts
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
            await _deliver(str(row["to_email"]), row["subject"], row["body"])
            await db.execute(
                """UPDATE email_outbox
                      SET status = 'delivered', attempts = $2, sent_at = now(), last_error = NULL
                    WHERE id = $1""",
                row["id"],
                row["attempts"] + 1,
            )
        except Exception as exc:
            attempts = row["attempts"] + 1
            dead = attempts >= MAX_ATTEMPTS
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
