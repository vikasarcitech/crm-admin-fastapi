"""Request models.

Pydantic replaces the hand-rolled coercion the Node version needed:
lengths, types and enums are enforced before a handler runs, and a bad
body produces a 422 instead of reaching SQL.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

WHITESPACE = re.compile(r"\s+")


def collapse(value: str | None, limit: int) -> str | None:
    """Trim, collapse runs of whitespace, cap length. Empty becomes None."""
    if value is None:
        return None
    cleaned = WHITESPACE.sub(" ", str(value)).strip()
    return cleaned[:limit] or None


def keep_lines(value: str | None, limit: int) -> str | None:
    """Same, but preserves line breaks (messages, notes)."""
    if value is None:
        return None
    cleaned = str(value).replace("\r\n", "\n").strip()
    return cleaned[:limit] or None


class LeadStatus(str, Enum):
    new = "new"
    contacted = "contacted"
    qualified = "qualified"
    proposal = "proposal"
    won = "won"
    lost = "lost"


class UserRole(str, Enum):
    owner = "owner"
    admin = "admin"
    agent = "agent"
    viewer = "viewer"


class BulkAction(str, Enum):
    status = "status"
    assign = "assign"
    spam = "spam"
    delete = "delete"


class LoginRequest(BaseModel):
    tenant: str | None = Field(default=None, max_length=60)
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class RegisterRequest(BaseModel):
    """Self-service sign-up: a new workspace with the registrant as owner."""

    workspace: str = Field(min_length=2, max_length=60)
    display_name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=200)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class LeadUpdate(BaseModel):
    """Every field optional; only what's sent gets written."""

    model_config = ConfigDict(extra="forbid")

    status: LeadStatus | None = None
    assigned_to: int | None = None
    follow_up_on: date | None = None
    full_name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=160)
    value_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999999"))
    is_spam: bool | None = None


class BulkRequest(BaseModel):
    ids: Annotated[list[int], Field(min_length=1, max_length=200)]
    action: BulkAction
    value: Any = None


class NoteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class UserCreate(BaseModel):
    email: str = Field(max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    role: UserRole
    password: str = Field(min_length=12, max_length=200)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=120)
    role: UserRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=500)
    events: list[str] = Field(default_factory=lambda: ["lead.created"], max_length=10)


class SettingUpdate(BaseModel):
    value: Any = None


PAGE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _page_slug(value: str) -> str:
    cleaned = value.strip().lower()
    if not PAGE_SLUG_RE.match(cleaned):
        raise ValueError("must be lowercase letters, numbers and dashes")
    return cleaned


class PageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=80)

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str) -> str:
        return _page_slug(value)


class PageUpdate(BaseModel):
    """Every field optional; blocks/theme get deep-validated in pagebuilder."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=160)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    blocks: list[Any] | None = None
    theme: dict[str, Any] | None = None

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str | None) -> str | None:
        return _page_slug(value) if value is not None else None


class IntakeMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_page: str | None = Field(default=None, max_length=500)
    landing_page: str | None = Field(default=None, max_length=500)
    referrer: str | None = Field(default=None, max_length=500)
    utm_source: str | None = Field(default=None, max_length=120)
    utm_medium: str | None = Field(default=None, max_length=120)
    utm_campaign: str | None = Field(default=None, max_length=160)
    utm_term: str | None = Field(default=None, max_length=160)
    utm_content: str | None = Field(default=None, max_length=160)


class IntakeRequest(BaseModel):
    """Core fields are named; anything else the form defines lands in extra."""

    model_config = ConfigDict(extra="allow")

    full_name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=160)
    message: str | None = Field(default=None, max_length=4000)

    hp: str | None = Field(default=None, alias="_hp", max_length=200)
    rendered_at: int | None = Field(default=None, alias="_t")
    captcha: str | None = Field(default=None, alias="_captcha", max_length=4000)
    meta: IntakeMeta = Field(default_factory=IntakeMeta)


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


def valid_email(value: str | None) -> str | None:
    """Permissive on purpose: reject obvious junk, let delivery prove the rest."""
    cleaned = collapse(value, 254)
    if not cleaned:
        return None
    return cleaned.lower() if EMAIL_RE.match(cleaned) else None
