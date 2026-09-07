"""Seed a tenant, owner account, default contact form and sample leads.

    OWNER_EMAIL=you@arcitech.ai OWNER_PASSWORD='...' python -m db.seed

Safe to re-run: everything is upserted by natural key, and sample leads
are only inserted into an empty pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from datetime import timedelta

import asyncpg
import bcrypt
from dotenv import load_dotenv

load_dotenv()

TENANT_SLUG = os.getenv("SEED_TENANT_SLUG", "demo")
TENANT_NAME = os.getenv("SEED_TENANT_NAME", "Demo Client")
EMAIL = os.getenv("OWNER_EMAIL", "admin@example.com")
PASSWORD = os.getenv("OWNER_PASSWORD", "ChangeMe!2026")

FORM_FIELDS = [
    {"name": "full_name", "label": "Name", "type": "text", "required": True, "max": 120},
    {"name": "email", "label": "Email", "type": "email", "required": True},
    {"name": "phone", "label": "Phone", "type": "tel", "required": False},
    {"name": "company", "label": "Company", "type": "text", "required": False},
    {"name": "message", "label": "How can we help?", "type": "textarea", "required": True, "max": 4000},
]

SAMPLE = [
    ("Priya Nair", "priya@northwind.co", "+971 50 118 2244", "Northwind FZ-LLC",
     "Need a corporate site rebuild before Q4.", "qualified", "google", "cpc", "uae-brand"),
    ("Marcus Feld", "m.feld@steelbridge.io", "+1 415 555 0142", "Steelbridge",
     "Interested in the data-centre landing pages.", "contacted", "linkedin", "social", "always-on"),
    ("Aisha Rahman", "aisha.r@gulfmed.ae", "+971 4 555 9910", "GulfMed",
     "Requesting a proposal for a 40-page site.", "proposal", "google", "organic", None),
    ("Tom Berensen", "tom@bereninc.com", None, "Beren Inc",
     "Pricing for ongoing maintenance?", "new", "direct", "none", None),
    ("Kavya Iyer", "kavya@proschool.in", "+91 98200 44112", "Proschool",
     "Landing page for the new course launch.", "won", "newsletter", "email", "course-launch"),
]


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set. Copy .env.example to .env first.", file=sys.stderr)
        return 1

    if len(PASSWORD) < 12:
        print("OWNER_PASSWORD must be at least 12 characters.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            tenant_id = await conn.fetchval(
                """INSERT INTO tenants (slug, name, primary_domain) VALUES ($1, $2, $3)
                   ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                TENANT_SLUG, TENANT_NAME, "localhost",
            )

            digest = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=12)).decode()
            owner_id = await conn.fetchval(
                """INSERT INTO users (tenant_id, email, password_hash, display_name, role)
                   VALUES ($1, $2, $3, $4, 'owner')
                   ON CONFLICT (tenant_id, email)
                   DO UPDATE SET password_hash = EXCLUDED.password_hash
                   RETURNING id""",
                tenant_id, EMAIL, digest, "Site Owner",
            )

            form_id = await conn.fetchval(
                """INSERT INTO forms (tenant_id, slug, name, fields, notify_emails)
                   VALUES ($1, 'contact', 'Contact form', $2::jsonb, $3)
                   ON CONFLICT (tenant_id, slug) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                tenant_id, json.dumps(FORM_FIELDS), [EMAIL],
            )

            existing = await conn.fetchval(
                "SELECT count(*) FROM leads WHERE tenant_id = $1", tenant_id
            )
            if existing == 0:
                for name, email, phone, company, message, status, src, medium, campaign in SAMPLE:
                    await conn.execute(
                        """INSERT INTO leads (tenant_id, form_id, full_name, email, phone, company,
                                              message, status, assigned_to, source_page,
                                              utm_source, utm_medium, utm_campaign, created_at)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8::lead_status, $9, '/contact',
                                   $10, $11, $12, now() - $13::interval)""",
                        tenant_id, form_id, name, email, phone, company, message, status,
                        owner_id, src, medium, campaign,
                        timedelta(days=random.uniform(0, 21)),
                    )

        print(f'Seeded tenant "{TENANT_SLUG}" (id {tenant_id}). Sign in as {EMAIL}')
        return 0
    except asyncpg.PostgresError as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
