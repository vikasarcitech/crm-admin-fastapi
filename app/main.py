"""FastAPI application wiring.

The JSON error shape is `{"error": "..."}` throughout, matching what the
SPA reads. FastAPI's default is `{"detail": ...}`, so both HTTPException
and validation errors are remapped below.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, events, mail, tenancy, workers
from .config import settings
from .routers import (
    admin,
    analytics,
    auth,
    compliance,
    content,
    deploy,
    forms,
    integrations,
    intake,
    leads,
    marketing,
    media,
    ops,
    pages,
    seo,
    site,
    sites,
    users,
)

logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("crm")

# ENV != production turns on DEBUG, which otherwise means every Pillow
# plugin import and asyncio selector detail lands in the app log.
for noisy in ("PIL", "asyncio", "httpx", "httpcore", "botocore", "boto3", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.fetch("SELECT 1")
    log.info("database ready")

    isolation = await db.rls_status()
    if isolation["effective"]:
        log.info(
            "tenant isolation: application scope + row-level security "
            "(role %s, %d policies)",
            isolation["role"], isolation["policies"],
        )
    else:
        log.warning("tenant isolation: application scope only — %s", isolation["warning"])

    tasks: list[asyncio.Task] = []
    if settings.run_workers:
        # Across several tasks or uvicorn workers, run these in ONE
        # dedicated process (RUN_WORKERS=0 elsewhere) rather than polling
        # from every replica.
        tasks.append(asyncio.create_task(events.webhook_worker()))
        tasks.append(asyncio.create_task(events.session_prune_worker()))
        tasks.append(asyncio.create_task(mail.email_worker()))
        # Platform workers: scheduled publishing, campaigns, build hooks
        # and CDN invalidation, health probes, retention and backups.
        for factory in workers.all_workers():
            tasks.append(asyncio.create_task(factory()))
        log.info("started %d background workers", len(tasks))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.disconnect()
        log.info("shutdown complete")


app = FastAPI(
    title="CRM Admin",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.debug else None,
)


# ------------------------------------------------------ security headers
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["x-content-type-options"] = "nosniff"
    # setdefault: rendered pages (public + editor preview) set their own
    # CSP and framing policy; everything else gets the strict defaults.
    response.headers.setdefault("x-frame-options", "DENY")
    response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
    response.headers["permissions-policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers.setdefault("content-security-policy", CSP)
    if settings.env == "production":
        response.headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
    return response


# ------------------------------------------------- CORS, scoped per site
# Only the public paths are cross-origin; the admin API stays
# same-origin. A blanket CORSMiddleware would open the authenticated
# API to every listed origin.
PUBLIC_PREFIXES = ("/api/public/", "/api/v1/")

# /api/public/{slug}/… and /api/v1/{slug}/… — the slug is the third
# segment. /api/v1/preview/{token} has no slug and is not cross-origin.
TENANT_IN_PATH = re.compile(r"^/api/(?:public|v1)/([a-z0-9][a-z0-9-]{0,59})(?:/|$)")


@app.middleware("http")
async def public_cors(request: Request, call_next):
    """Allow a site's own verified domains, not one shared list.

    The original single PUBLIC_FORM_ORIGINS list was install-wide,
    which in a multi-site platform means any client's frontend could
    post to any other client's forms. Origins now come from the
    resolved site's verified domains; PUBLIC_FORM_ORIGINS remains as a
    development escape hatch and for frontends whose domain is not
    registered yet.
    """
    path = request.url.path
    is_public = path.startswith(PUBLIC_PREFIXES)
    origin = request.headers.get("origin")

    allowed = False
    if origin and is_public:
        if "*" in settings.public_form_origins:
            allowed = True
        elif origin in settings.public_form_origins:
            allowed = True
        else:
            match = TENANT_IN_PATH.match(path)
            if match:
                try:
                    tenant = await tenancy.by_slug(match.group(1))
                    if tenant and tenant["is_active"]:
                        allowed = origin in await tenancy.allowed_origins(tenant["id"])
                except Exception as exc:
                    # A resolution failure must not turn every public
                    # request into a 500; it just means "not allowed".
                    log.error("CORS origin check failed for %s: %s", path, exc)

    if request.method == "OPTIONS" and is_public:
        response = Response(status_code=204)
    else:
        response = await call_next(request)

    if allowed:
        response.headers["access-control-allow-origin"] = origin
        response.headers["vary"] = "Origin"
        response.headers["access-control-allow-headers"] = "content-type"
        response.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
        response.headers["access-control-max-age"] = "86400"
    elif is_public:
        # Say why, once, in the log — a silent CORS failure is the
        # single most time-consuming thing to debug from the browser.
        if origin:
            log.info("CORS refused origin %s for %s", origin, path)
        response.headers["vary"] = "Origin"
    return response


# ------------------------------------------------------- error responses
@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", [])[1:]) or "request"
    return JSONResponse(
        status_code=400,
        content={"error": f"{field}: {first.get('msg', 'is not valid')}"},
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # Detail stays in the logs; the client gets a stable, non-leaky message.
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "Something went wrong on our side."})


# ---------------------------------------------------------------- routes
@app.get("/healthz")
async def healthz() -> dict:
    """Liveness, plus whether database-enforced isolation is in force.

    Reported here so a deployment check can assert it rather than
    discovering months later that the app connects as a superuser and
    every RLS policy has been inert the whole time.
    """
    isolation = await db.rls_status()
    return {
        "ok": True,
        "isolation": {
            "applicationScope": True,
            "rowLevelSecurity": isolation["effective"],
            "warning": isolation["warning"],
        },
    }


app.include_router(intake.router)  # unauthenticated
app.include_router(auth.router)
app.include_router(leads.router)
app.include_router(pages.router)
app.include_router(pages.public_router)  # published pages, unauthenticated
app.include_router(content.router)
app.include_router(seo.router)
app.include_router(seo.public_router)  # sitemap, robots, redirect lookup
app.include_router(media.router)
app.include_router(media.public_router)  # local media serving
app.include_router(users.router)
app.include_router(sites.router)  # multi-site control plane
app.include_router(site.router)
app.include_router(site.public_router)  # public site config, menus
app.include_router(forms.router)
app.include_router(forms.templates_router)
app.include_router(forms.conversions_router)
app.include_router(forms.public_router)  # conversion beacon
app.include_router(marketing.router)
app.include_router(marketing.public_router)  # subscribe, confirm, unsubscribe, short links
app.include_router(analytics.router)
app.include_router(analytics.public_router)  # page-view beacon
app.include_router(deploy.router)
app.include_router(deploy.public_router)  # versioned public content API
app.include_router(integrations.router)
app.include_router(integrations.public_router)  # OAuth callback
app.include_router(ops.router)
app.include_router(compliance.router)
app.include_router(compliance.public_router)  # cookie/consent capture
app.include_router(admin.router)


# ------------------------------------------------------------ static SPA
app.mount("/css", StaticFiles(directory=PUBLIC_DIR / "css"), name="css")
app.mount("/js", StaticFiles(directory=PUBLIC_DIR / "js"), name="js")


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "login.html")


@app.get("/register", include_in_schema=False)
async def register_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "register.html")


@app.get("/forgot", include_in_schema=False)
async def forgot_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "forgot.html")


@app.get("/reset", include_in_schema=False)
async def reset_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "reset.html")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str) -> FileResponse:
    """Serve the admin shell for any non-API path."""
    candidate = (PUBLIC_DIR / full_path).resolve()
    if (
        full_path
        and candidate.is_file()
        and candidate.is_relative_to(PUBLIC_DIR)  # no traversal out of public/
    ):
        return FileResponse(candidate)
    return FileResponse(PUBLIC_DIR / "index.html")
