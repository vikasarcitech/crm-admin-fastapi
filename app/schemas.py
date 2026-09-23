"""Request models.

Pydantic replaces the hand-rolled coercion the Node version needed:
lengths, types and enums are enforced before a handler runs, and a bad
body produces a 422 instead of reaching SQL.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
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
    """The five platform roles, plus the two CRM roles the original
    schema shipped with. Both sets stay valid: 'agent' and 'viewer' are
    lead-desk roles, the rest are content roles. What each one may
    actually do is defined in app/permissions.py, not by this order."""

    super_admin = "super_admin"
    owner = "owner"
    admin = "admin"
    editor = "editor"
    author = "author"
    contributor = "contributor"
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


class ForgotRequest(BaseModel):
    tenant: str | None = Field(default=None, max_length=60)
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class ResetRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=12, max_length=200)


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
    """Normalise a slug the way the editor does, then insist on the result.

    Spaces, underscores and case are corrected rather than rejected —
    "About Us" is obviously "about-us", and a client that did not run
    the editor's slugify should not be told off for it. Only what cannot
    be fixed (nothing left after folding, e.g. an all-emoji title) is an
    error, and the message says what to do.
    """
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:80]
    if not cleaned:
        raise ValueError("needs at least one letter or number")
    if not PAGE_SLUG_RE.match(cleaned):
        raise ValueError("must be lowercase letters, numbers and dashes")
    return cleaned


class PageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=80)
    # Meta at creation, not only in page settings: the description and
    # the SEO title are what a page needs before it is shared, and
    # asking for them once is cheaper than remembering to go back.
    description: str | None = Field(default=None, max_length=300)
    seo: dict[str, Any] | None = None
    # What the page starts as: the block starter, or a single HTML block
    # for someone who is writing the markup themselves.
    starter: str | None = Field(default=None, pattern="^(blocks|html)$")

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
    seo: dict[str, Any] | None = None

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str | None) -> str | None:
        return _page_slug(value) if value is not None else None


class PageModeChange(BaseModel):
    """Switch a page between the block builder and hand-written HTML.

    ``html`` is the markup to start from, and it is how the builder's
    whole-page source view saves: the author has the page's HTML in
    front of them and has edited it, so converting must keep *their*
    text rather than re-render the blocks and throw it away. Omitted,
    the server renders the existing blocks as before.
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(pattern="^(blocks|html)$")
    html: str | None = Field(default=None, max_length=200_000)


class PageHtmlCheck(BaseModel):
    """A raw HTML fragment the block editor wants dry-run through the cleaner.

    The cap here is deliberately looser than the block's own
    ``MAX_HTML_CHARS``: an over-long paste should come back with the
    cleaner's own "too large" message, not a schema error the editor
    cannot explain.
    """

    model_config = ConfigDict(extra="forbid")

    html: str = Field(default="", max_length=500_000)
    # True when the editor is showing the whole page rather than one
    # block: only there can the markup be a whole HTML document, so only
    # there does the answer change.
    whole_page: bool = False


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


# =====================================================================
# 2.1  CONTENT MANAGEMENT
# ---------------------------------------------------------------------
# Deep validation of blocks, field schemas, rich text and SEO lives in
# app/content.py and app/sanitize.py: it depends on the content type's
# own schema, which Pydantic cannot know at class-definition time. The
# models here enforce shape, type and length before that runs.
# =====================================================================


class ContentStatus(str, Enum):
    draft = "draft"
    published = "published"
    scheduled = "scheduled"
    trashed = "trashed"


class ContentTypeKind(str, Enum):
    collection = "collection"
    single = "single"


class ContentTypeCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=80)
    plural_name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    kind: ContentTypeKind = ContentTypeKind.collection
    route_prefix: str | None = Field(default=None, max_length=80)
    field_schema: list[Any] | None = None
    supports: dict[str, Any] | None = None
    icon: str | None = Field(default=None, max_length=40)


class ContentTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    plural_name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    route_prefix: str | None = Field(default=None, max_length=80)
    field_schema: list[Any] | None = None
    supports: dict[str, Any] | None = None
    icon: str | None = Field(default=None, max_length=40)
    sort_order: int | None = Field(default=None, ge=0, le=999)
    is_active: bool | None = None


class ContentCreate(BaseModel):
    type: str = Field(min_length=1, max_length=60)          # content type slug
    title: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=80)   # derived when absent
    excerpt: str | None = Field(default=None, max_length=600)
    body: str | None = None
    fields: dict[str, Any] | None = None
    seo: dict[str, Any] | None = None
    author_id: int | None = None
    featured_media_id: int | None = None
    term_ids: list[int] | None = Field(default=None, max_length=60)
    status: ContentStatus = ContentStatus.draft
    scheduled_for: datetime | None = None
    menu_order: int | None = Field(default=None, ge=-9999, le=9999)


class ContentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=80)
    excerpt: str | None = Field(default=None, max_length=600)
    body: str | None = None
    fields: dict[str, Any] | None = None
    seo: dict[str, Any] | None = None
    author_id: int | None = None
    featured_media_id: int | None = None
    term_ids: list[int] | None = Field(default=None, max_length=60)
    menu_order: int | None = Field(default=None, ge=-9999, le=9999)
    # Slug changes normally leave a 301 behind; this opts out.
    skip_redirect: bool = False


class PublishRequest(BaseModel):
    """No date publishes now; a future date schedules."""

    scheduled_for: datetime | None = None
    note: str | None = Field(default=None, max_length=200)


class ContentBulkAction(str, Enum):
    publish = "publish"
    unpublish = "unpublish"
    trash = "trash"
    restore = "restore"
    delete = "delete"
    add_terms = "add_terms"
    remove_terms = "remove_terms"
    set_author = "set_author"


class ContentBulkRequest(BaseModel):
    ids: Annotated[list[int], Field(min_length=1, max_length=200)]
    action: ContentBulkAction
    term_ids: list[int] | None = Field(default=None, max_length=60)
    author_id: int | None = None


class RevisionRestore(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class TaxonomyCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=80)
    plural_name: str | None = Field(default=None, max_length=80)
    is_hierarchical: bool = False
    type_slugs: list[str] | None = Field(default=None, max_length=30)


class TaxonomyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    plural_name: str | None = Field(default=None, max_length=80)
    type_slugs: list[str] | None = Field(default=None, max_length=30)


class TermCreate(BaseModel):
    taxonomy: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=600)
    parent_id: int | None = None
    seo: dict[str, Any] | None = None
    sort_order: int | None = Field(default=None, ge=-9999, le=9999)


class TermUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=600)
    parent_id: int | None = None
    seo: dict[str, Any] | None = None
    sort_order: int | None = Field(default=None, ge=-9999, le=9999)


# =====================================================================
# 2.2  SEO & SITE DISCOVERY
# =====================================================================


class RedirectCreate(BaseModel):
    from_path: str = Field(min_length=1, max_length=500)
    to_path: str = Field(min_length=1, max_length=500)
    status_code: int = Field(default=301)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("status_code")
    @classmethod
    def check_code(cls, value: int) -> int:
        if value not in (301, 302, 307, 308):
            raise ValueError("must be 301, 302, 307 or 308")
        return value


class RedirectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_path: str | None = Field(default=None, min_length=1, max_length=500)
    status_code: int | None = None
    is_active: bool | None = None
    note: str | None = Field(default=None, max_length=200)

    @field_validator("status_code")
    @classmethod
    def check_code(cls, value: int | None) -> int | None:
        if value is not None and value not in (301, 302, 307, 308):
            raise ValueError("must be 301, 302, 307 or 308")
        return value


class NotFoundResolve(BaseModel):
    """Turn a logged 404 straight into a redirect."""

    to_path: str = Field(min_length=1, max_length=500)
    status_code: int = 301


# =====================================================================
# 2.3  MEDIA MANAGEMENT
# =====================================================================


class MediaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alt_text: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=200)
    caption: str | None = Field(default=None, max_length=600)
    folder_id: int | None = None
    tags: list[str] | None = Field(default=None, max_length=25)


class MediaBulkAction(str, Enum):
    move = "move"
    tag = "tag"
    untag = "untag"
    trash = "trash"
    restore = "restore"


class MediaBulkRequest(BaseModel):
    ids: Annotated[list[int], Field(min_length=1, max_length=200)]
    action: MediaBulkAction
    folder_id: int | None = None
    tags: list[str] | None = Field(default=None, max_length=25)


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: int | None = None


class FolderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    parent_id: int | None = None


# =====================================================================
# 2.4  USERS, ROLES & SECURITY
# =====================================================================


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    bio: str | None = Field(default=None, max_length=1000)
    job_title: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    avatar_media_id: int | None = None
    social: dict[str, str] | None = None
    locale: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=60)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)


class TotpVerify(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TotpDisable(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class TwoFactorLogin(BaseModel):
    """Second step of a login that owes a code."""

    challenge: str = Field(min_length=10, max_length=200)
    code: str = Field(min_length=6, max_length=20)


class RolePermissionUpdate(BaseModel):
    role: str = Field(min_length=1, max_length=30)
    permission: str = Field(min_length=1, max_length=60)
    # null clears the override and restores the role default
    allowed: bool | None = None


class MembershipCreate(BaseModel):
    """Grant an existing user access to another site."""

    user_email: str = Field(max_length=254)
    tenant_slug: str = Field(max_length=60)
    role: UserRole | str = "editor"

    @field_validator("user_email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


# =====================================================================
# 2.5  SITE STRUCTURE & GLOBAL SETTINGS
# =====================================================================


class MenuCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str | None = Field(default=None, max_length=60)
    location: str | None = Field(default=None, max_length=40)


class MenuItemInput(BaseModel):
    """One node of a menu tree. Children nest through `children`, which
    is how the drag-and-drop builder posts a whole reordered menu."""

    id: int | None = None
    label: str = Field(min_length=1, max_length=120)
    link_type: str = Field(default="custom", max_length=20)
    url: str | None = Field(default=None, max_length=500)
    object_id: int | None = None
    target: str | None = Field(default=None, max_length=10)
    rel: str | None = Field(default=None, max_length=80)
    icon: str | None = Field(default=None, max_length=40)
    is_active: bool = True
    children: list[MenuItemInput] | None = Field(default=None, max_length=100)

    @field_validator("link_type")
    @classmethod
    def check_link_type(cls, value: str) -> str:
        if value not in {"custom", "content", "term", "page"}:
            raise ValueError("must be custom, content, term or page")
        return value

    @field_validator("target")
    @classmethod
    def check_target(cls, value: str | None) -> str | None:
        if value is not None and value not in {"_self", "_blank"}:
            raise ValueError("must be _self or _blank")
        return value


class MenuTreeUpdate(BaseModel):
    """Replaces the whole tree in one request — a partial reorder would
    need per-node parent/index patches and could leave an orphan."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    location: str | None = Field(default=None, max_length=40)
    items: list[MenuItemInput] = Field(default_factory=list, max_length=200)


class BlockCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str | None = Field(default=None, max_length=60)
    kind: str = Field(default="html", max_length=20)
    content: dict[str, Any] | None = None


class BlockUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: str | None = Field(default=None, max_length=20)
    content: dict[str, Any] | None = None
    is_active: bool | None = None


# =====================================================================
# 2.7  FORMS & CONVERSION
# =====================================================================


class FormFieldInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(default="text", max_length=20)
    required: bool = False
    max: int | None = Field(default=None, ge=1, le=100_000)
    placeholder: str | None = Field(default=None, max_length=120)
    help: str | None = Field(default=None, max_length=200)
    options: list[str] | None = Field(default=None, max_length=60)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]{0,39}$", cleaned):
            raise ValueError("must be lowercase letters, digits and underscores")
        return cleaned

    @field_validator("type")
    @classmethod
    def check_type(cls, value: str) -> str:
        allowed = {
            "text", "email", "tel", "textarea", "number", "url", "date",
            "select", "radio", "checkbox", "hidden", "consent",
        }
        if value not in allowed:
            raise ValueError(f"must be one of: {', '.join(sorted(allowed))}")
        return value


class FormCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=60)
    fields: list[FormFieldInput] | None = Field(default=None, max_length=40)
    notify_emails: list[str] | None = Field(default=None, max_length=20)


class FormUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=60)
    fields: list[FormFieldInput] | None = Field(default=None, max_length=40)
    notify_emails: list[str] | None = Field(default=None, max_length=20)
    settings: dict[str, Any] | None = None
    lead_mapping: dict[str, str] | None = None
    notification_rules: list[Any] | None = Field(default=None, max_length=20)
    autoresponder: dict[str, Any] | None = None
    is_active: bool | None = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=60)
    subject: str = Field(min_length=1, max_length=300)
    body_text: str = Field(min_length=1, max_length=40_000)
    body_html: str | None = Field(default=None, max_length=200_000)
    kind: str = Field(default="transactional", max_length=20)


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    body_text: str | None = Field(default=None, min_length=1, max_length=40_000)
    body_html: str | None = Field(default=None, max_length=200_000)
    kind: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None


class ConversionKind(str, Enum):
    form = "form"
    cta = "cta"
    phone = "phone"
    email = "email"
    whatsapp = "whatsapp"
    download = "download"
    custom = "custom"


class ConversionEvent(BaseModel):
    """Posted by the public site when a visitor converts."""

    model_config = ConfigDict(extra="ignore")

    kind: ConversionKind
    name: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=160)
    value_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999999"))
    meta: dict[str, Any] | None = None
    page: IntakeMeta = Field(default_factory=lambda: IntakeMeta())


# =====================================================================
# 2.8  MARKETING & NEWSLETTER
# =====================================================================


class SubscriberStatus(str, Enum):
    pending = "pending"
    subscribed = "subscribed"
    unsubscribed = "unsubscribed"
    bounced = "bounced"
    complained = "complained"


class SubscriberCreate(BaseModel):
    email: str = Field(max_length=254)
    name: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = Field(default=None, max_length=25)
    source: str | None = Field(default=None, max_length=80)
    # Admin-added subscribers can skip double opt-in; public sign-ups
    # never can (the public route ignores this field).
    confirmed: bool = False

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class SubscriberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    status: SubscriberStatus | None = None
    tags: list[str] | None = Field(default=None, max_length=25)


class SubscriberImport(BaseModel):
    """CSV or newline-separated 'email,name' rows pasted into the admin."""

    rows: str = Field(min_length=1, max_length=500_000)
    tags: list[str] | None = Field(default=None, max_length=25)
    confirmed: bool = False


class PublicSubscribe(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(max_length=254)
    name: str | None = Field(default=None, max_length=120)
    consent: bool = True
    hp: str | None = Field(default=None, alias="_hp", max_length=200)
    source_page: str | None = Field(default=None, max_length=500)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=300)
    preheader: str | None = Field(default=None, max_length=200)
    body_text: str = Field(min_length=1, max_length=200_000)
    body_html: str | None = Field(default=None, max_length=400_000)
    from_name: str | None = Field(default=None, max_length=120)
    from_email: str | None = Field(default=None, max_length=254)
    audience: dict[str, Any] | None = None


class CampaignUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    preheader: str | None = Field(default=None, max_length=200)
    body_text: str | None = Field(default=None, min_length=1, max_length=200_000)
    body_html: str | None = Field(default=None, max_length=400_000)
    from_name: str | None = Field(default=None, max_length=120)
    from_email: str | None = Field(default=None, max_length=254)
    audience: dict[str, Any] | None = None


class CampaignSchedule(BaseModel):
    """No date sends immediately; a future date queues it."""

    scheduled_for: datetime | None = None


class AnnouncementCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="bar", max_length=20)
    content: dict[str, Any] | None = None
    placement: dict[str, Any] | None = None
    priority: int = Field(default=0, ge=-999, le=999)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value: str) -> str:
        if value not in {"bar", "popup", "banner", "slide-in"}:
            raise ValueError("must be bar, popup, banner or slide-in")
        return value


class AnnouncementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: str | None = Field(default=None, max_length=20)
    content: dict[str, Any] | None = None
    placement: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=-999, le=999)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None


class UtmLinkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    utm_source: str = Field(min_length=1, max_length=120)
    utm_medium: str = Field(min_length=1, max_length=120)
    utm_campaign: str = Field(min_length=1, max_length=160)
    utm_term: str | None = Field(default=None, max_length=160)
    utm_content: str | None = Field(default=None, max_length=160)


# =====================================================================
# 2.9 / 2.10  ANALYTICS, DEPLOYMENT & PUBLISHING
# =====================================================================


class PageViewBeacon(BaseModel):
    """Aggregate-only analytics beacon from the public site."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1, max_length=500)
    referrer: str | None = Field(default=None, max_length=500)
    utm_source: str | None = Field(default=None, max_length=120)
    utm_medium: str | None = Field(default=None, max_length=120)
    utm_campaign: str | None = Field(default=None, max_length=160)
    # Client-generated, rotates per session; never an identifier we mint.
    session_id: str | None = Field(default=None, max_length=64)
    is_new_session: bool = True


class BuildHookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=500)
    provider: str = Field(default="generic", max_length=20)
    auth_token: str | None = Field(default=None, max_length=500)
    trigger_events: list[str] | None = Field(default=None, max_length=10)
    debounce_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("provider")
    @classmethod
    def check_provider(cls, value: str) -> str:
        allowed = {"vercel", "netlify", "github", "cloudflare", "generic"}
        if value not in allowed:
            raise ValueError(f"must be one of: {', '.join(sorted(allowed))}")
        return value


class BuildHookUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    auth_token: str | None = Field(default=None, max_length=500)
    trigger_events: list[str] | None = Field(default=None, max_length=10)
    debounce_seconds: int | None = Field(default=None, ge=0, le=3600)
    is_active: bool | None = None


class BuildTrigger(BaseModel):
    hook_id: int | None = None      # omit to fire every active hook
    reason: str | None = Field(default=None, max_length=200)


class InvalidationRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] | None = Field(default=None, max_length=12)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    note: str | None = Field(default=None, max_length=200)


# =====================================================================
# 2.11 / 2.12  OPERATIONS & COMPLIANCE
# =====================================================================


class BackupRequest(BaseModel):
    kind: str = Field(default="database", max_length=20)

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value: str) -> str:
        if value not in {"database", "media", "full"}:
            raise ValueError("must be database, media or full")
        return value


class HealthCheckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=500)
    expect_status: int = Field(default=200, ge=100, le=599)
    expect_text: str | None = Field(default=None, max_length=200)
    interval_seconds: int = Field(default=300, ge=60, le=86400)


class HealthCheckUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    expect_status: int | None = Field(default=None, ge=100, le=599)
    expect_text: str | None = Field(default=None, max_length=200)
    interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    is_active: bool | None = None


class ConsentRecordInput(BaseModel):
    """Posted by the public site's cookie banner or a form's consent box."""

    model_config = ConfigDict(extra="ignore")

    purpose: str = Field(min_length=1, max_length=80)
    granted: bool
    email: str | None = Field(default=None, max_length=254)
    policy_version: str | None = Field(default=None, max_length=40)
    source_page: str | None = Field(default=None, max_length=500)
    evidence: dict[str, Any] | None = None


class DataRequestCreate(BaseModel):
    kind: str = Field(max_length=20)
    subject_email: str = Field(max_length=254)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value: str) -> str:
        if value not in {"export", "deletion", "rectification"}:
            raise ValueError("must be export, deletion or rectification")
        return value

    @field_validator("subject_email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class RetentionPolicyUpdate(BaseModel):
    scope: str = Field(max_length=40)
    days: int = Field(ge=1, le=36500)
    action: str = Field(default="delete", max_length=20)
    is_active: bool = True

    @field_validator("action")
    @classmethod
    def check_action(cls, value: str) -> str:
        if value not in {"delete", "anonymize"}:
            raise ValueError("must be delete or anonymize")
        return value


# =====================================================================
# MULTI-SITE CONTROL PLANE
# ---------------------------------------------------------------------
# Slug, domain and limit-key validation lives in app/tenancy.py, where
# it can check reserved names and what is already claimed across the
# install. These models enforce shape and length first.
# =====================================================================


class TenantStatus(str, Enum):
    active = "active"
    suspended = "suspended"
    archived = "archived"


class SiteCreate(BaseModel):
    """Provision a new site with its first owner."""

    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=60)
    domain: str | None = Field(default=None, max_length=253)
    owner_email: str = Field(max_length=254)
    owner_name: str | None = Field(default=None, max_length=120)
    # Same floor as every other password on the platform.
    owner_password: str = Field(min_length=12, max_length=200)
    plan: str = Field(default="standard", max_length=40)
    limits: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("owner_email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class SiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    plan: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=1000)
    # Per-site infrastructure overrides (bucket prefix, CDN id, build
    # target) — how one busy site is scaled without a platform change.
    infra: dict[str, Any] | None = None


class SiteStatusUpdate(BaseModel):
    status: TenantStatus
    reason: str | None = Field(default=None, max_length=500)


class SiteLimitsUpdate(BaseModel):
    """Replaces the whole override object; an omitted key falls back to
    the platform default rather than staying at its old value."""

    limits: dict[str, Any] = Field(default_factory=dict)


class DomainCreate(BaseModel):
    domain: str = Field(min_length=3, max_length=253)
    make_primary: bool = False


# =====================================================================
# INTEGRATIONS / CONNECTORS
# ---------------------------------------------------------------------
# Which config and credential keys a provider accepts is declared in
# app/connectors/registry.py and validated against that descriptor, so
# these models enforce only shape and length.
# =====================================================================


class ConnectorKind(str, Enum):
    crm = "crm"
    email = "email"
    automation = "automation"
    analytics = "analytics"
    storage = "storage"


class ConnectorCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    name: str | None = Field(default=None, max_length=120)
    config: dict[str, Any] | None = None
    # Secrets. Encrypted before they touch the database and never read
    # back out through the API.
    credentials: dict[str, Any] | None = None
    field_mapping: dict[str, str] | None = None
    events: list[str] | None = Field(default=None, max_length=25)


class ConnectorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict[str, Any] | None = None
    # Omitted keeps what is stored; a key present with an empty value
    # clears it. There is no way to read the current value back.
    credentials: dict[str, Any] | None = None
    field_mapping: dict[str, str] | None = None
    events: list[str] | None = Field(default=None, max_length=25)
    is_active: bool | None = None


class ConnectorTestPayload(BaseModel):
    """Sample lead used to preview a mapping without sending anything."""

    model_config = ConfigDict(extra="allow")

    full_name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=160)
    message: str | None = Field(default=None, max_length=4000)


class ConnectorReplay(BaseModel):
    """Re-send a delivery. Off by default because a replay can create a
    second record in the destination when the first actually landed."""

    delivery_id: int
