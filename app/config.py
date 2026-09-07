"""Configuration, read once at import from the environment.

In AWS these come from Secrets Manager or SSM Parameter Store at task
start, never from a baked image or a plaintext task definition.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _csv(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


class Settings:
    env: str = os.getenv("ENV", "development")
    debug: bool = os.getenv("ENV", "development") != "production"

    database_url: str = os.getenv("DATABASE_URL", "")
    pg_pool_min: int = int(os.getenv("PG_POOL_MIN", "2"))
    pg_pool_max: int = int(os.getenv("PG_POOL_MAX", "10"))
    pg_ssl: str = os.getenv("PGSSL", "disable")

    # Number of proxy hops in front of the app (ALB = 1, CloudFront + ALB = 2).
    # Set this wrong and a spoofed X-Forwarded-For defeats rate limiting.
    trust_proxy_hops: int = int(os.getenv("TRUST_PROXY_HOPS", "1"))

    session_days: int = int(os.getenv("SESSION_DAYS", "7"))
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # Self-service sign-up creates a brand-new workspace (tenant + owner).
    # Set ALLOW_SIGNUPS=0 on installs that should stay invite-only.
    allow_signups: bool = os.getenv("ALLOW_SIGNUPS", "1") == "1"

    public_form_origins: list[str] = _csv("PUBLIC_FORM_ORIGINS")
    turnstile_secret: str = os.getenv("TURNSTILE_SECRET", "")

    webhook_poll_seconds: int = int(os.getenv("WEBHOOK_POLL_SECONDS", "15"))
    run_workers: bool = os.getenv("RUN_WORKERS", "1") == "1"

    @property
    def cookie_secure(self) -> bool:
        return self.env == "production"


settings = Settings()
