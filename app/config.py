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

    # Outbound email. 'log' prints to the app log (dev); 'smtp' relays via
    # any SMTP provider — Amazon SES SMTP credentials in production.
    email_provider: str = os.getenv("EMAIL_PROVIDER", "log")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    email_from: str = os.getenv("EMAIL_FROM", "CRM Admin <no-reply@localhost>")
    email_poll_seconds: int = int(os.getenv("EMAIL_POLL_SECONDS", "10"))
    # Absolute origin for links in emails (password reset). When empty the
    # request's own host is used, which is fine behind a single domain.
    app_base_url: str = os.getenv("APP_BASE_URL", "").rstrip("/")


    # Hosts whose iframes survive HTML sanitization (app/sanitize.py).
    # Anything not listed here is stripped from rich-text content.
    embed_allowed_hosts: tuple[str, ...] = tuple(
        _csv("EMBED_ALLOWED_HOSTS")
        or (
            "youtube.com", "youtube-nocookie.com", "player.vimeo.com",
            "open.spotify.com", "w.soundcloud.com", "google.com",
            "calendly.com", "maps.google.com",
        )
    )


    # ---------------------------------------------------------- media
    # 'local' writes under MEDIA_ROOT (dev; a container filesystem is
    # ephemeral). 's3' is the production path — pair it with CloudFront
    # via MEDIA_PUBLIC_BASE_URL and keep the bucket private.
    media_storage: str = os.getenv("MEDIA_STORAGE", "local")
    media_root: str = os.getenv("MEDIA_ROOT", "var/media")
    media_s3_bucket: str = os.getenv("MEDIA_S3_BUCKET", "")
    media_s3_prefix: str = os.getenv("MEDIA_S3_PREFIX", "media")
    media_public_base_url: str = os.getenv("MEDIA_PUBLIC_BASE_URL", "").rstrip("/")
    aws_region: str = os.getenv("AWS_REGION", "")

    # --------------------------------------------------------- workers
    # Which background workers this process runs. Blank or "all" runs
    # every one (right for a single install). A comma-separated subset
    # — "media", "connectors,builds" — is how heavy jobs are moved onto
    # their own service. See app/workers.py WORKERS for the names.
    workers: str = os.getenv("WORKERS", "")

    content_poll_seconds: int = int(os.getenv("CONTENT_POLL_SECONDS", "30"))
    build_poll_seconds: int = int(os.getenv("BUILD_POLL_SECONDS", "15"))
    health_poll_seconds: int = int(os.getenv("HEALTH_POLL_SECONDS", "60"))
    campaign_batch_size: int = int(os.getenv("CAMPAIGN_BATCH_SIZE", "50"))
    # Retention and health sweeps are hourly; 0 disables them entirely.
    retention_hour_interval: int = int(os.getenv("RETENTION_HOUR_INTERVAL", "6"))

    # ------------------------------------------------------ deployment
    # CloudFront distribution to invalidate after a publish. Blank keeps
    # invalidation queued-but-skipped, which is right for a pull CDN.
    cloudfront_distribution_id: str = os.getenv("CLOUDFRONT_DISTRIBUTION_ID", "")
    # Public origin of the *website* (not the admin), used to build
    # absolute URLs in sitemaps and canonical tags.
    site_base_url: str = os.getenv("SITE_BASE_URL", "").rstrip("/")

    # --------------------------------------------------------- backups
    backup_s3_bucket: str = os.getenv("BACKUP_S3_BUCKET", "")
    backup_s3_prefix: str = os.getenv("BACKUP_S3_PREFIX", "backups")
    # pg_dump must be on PATH for database backups to run.
    pg_dump_path: str = os.getenv("PG_DUMP_PATH", "pg_dump")

    # --------------------------------------------------- integrations
    # Fernet key(s) encrypting connector credentials at rest. Comma
    # separated for rotation: the first encrypts, all of them decrypt.
    # Generate one with:
    #   python -c "from app.crypto import generate_key; print(generate_key())"
    # Without it, connectors that need a secret refuse to save rather
    # than storing it in the clear.
    credentials_key: str = os.getenv("CREDENTIALS_KEY", "")

    # Absolute origin the OAuth providers redirect back to. Must match
    # the callback URL registered with each provider exactly.
    oauth_redirect_base: str = os.getenv(
        "OAUTH_REDIRECT_BASE", os.getenv("APP_BASE_URL", "")
    ).rstrip("/")

    connector_poll_seconds: int = int(os.getenv("CONNECTOR_POLL_SECONDS", "10"))
    # Media derivative generation. Polls faster while a backlog
    # exists, so this is the idle interval.
    media_poll_seconds: int = int(os.getenv("MEDIA_POLL_SECONDS", "5"))

    # ----------------------------------------------- read replicas
    # Comma-separated DSNs. Reads that explicitly opt in (reporting,
    # dashboards) go here; everything else uses the writer. Empty means
    # every read is on the writer, which is right until read volume
    # justifies the replication lag.
    replica_urls: list[str] = _csv("DATABASE_REPLICA_URLS")

    # ------------------------------------------------------ caching
    # Cross-process cache invalidation over LISTEN/NOTIFY. With it off,
    # a permission or site change takes up to the cache TTL to reach
    # every replica instead of milliseconds.
    cache_notify: bool = os.getenv("CACHE_NOTIFY", "1") == "1"

    # ------------------------------------------------ rate limiting
    # 'memory' counts per process, so with several workers or tasks the
    # effective limit is multiplied by the process count — correct only
    # for single-process development. 'postgres' shares the counters
    # with no new infrastructure; 'redis' when the write volume
    # outgrows that. /healthz reports which is in use.
    rate_limit_backend: str = os.getenv("RATE_LIMIT_BACKEND", "memory")
    redis_url: str = os.getenv("REDIS_URL", "")

    # ----------------------------------------------------- isolation
    # Declare the tenant on the connection so the row-level-security
    # policies in db/tenancy.sql apply. Costs one extra round trip per
    # scoped query. 0 falls back to the bound-$1 scoping alone.
    # Only bites when DATABASE_URL uses a non-superuser, non-BYPASSRLS
    # role — /healthz reports whether it is actually in force.
    pg_rls: bool = os.getenv("PG_RLS", "1") == "1"

    # -------------------------------------------------------- privacy
    # Salt for the rotating visitor hash used by the analytics beacon.
    # Rotate it to sever any link between old and new visitor counts.
    analytics_salt: str = os.getenv("ANALYTICS_SALT", "")

    @property
    def cookie_secure(self) -> bool:
        return self.env == "production"


settings = Settings()
