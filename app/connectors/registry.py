"""What each provider is, declared as data.

Adding a service should be a registry entry plus one class, not a
change to the router, the queue, the worker or the UI. Everything the
admin renders — the setup form, the OAuth button, the field-mapping
picker — is generated from these descriptors, so a new provider gets a
working configuration screen for free.

A descriptor answers five questions:

  how do we authenticate?   auth + oauth/credential_fields
  what does it need to know? config_fields
  what can it receive?       events
  what fields can we write?  targets
  what should it map by default? default_mapping
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Field:
    """One input on the connector's setup form."""

    name: str
    label: str
    type: str = "text"          # text | password | select | url | textarea
    required: bool = False
    help: str | None = None
    options: tuple[tuple[str, str], ...] = ()
    default: Any = None
    # Secrets go in `credentials` (encrypted); everything else in
    # `config` (plain, shown in the UI).
    secret: bool = False


@dataclass(frozen=True, slots=True)
class OAuth:
    """OAuth 2.0 authorization-code details.

    `authorize_url` and `token_url` may contain `{dc}` for providers
    whose endpoints are per-region (Zoho) — resolved from config at
    request time.
    """

    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    # Zoho needs access_type=offline&prompt=consent or it returns no
    # refresh token — and the second connection silently has no way to
    # renew itself.
    extra_authorize_params: tuple[tuple[str, str], ...] = ()
    uses_pkce: bool = False
    # Providers that return the API host in the token response rather
    # than having one fixed host (Salesforce, Zoho).
    instance_from_token: str | None = None


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    kind: str                    # crm | email | automation | analytics | storage
    label: str
    summary: str
    auth: str = "api_key"        # oauth2 | api_key | none
    docs_url: str | None = None
    config_fields: tuple[Field, ...] = ()
    credential_fields: tuple[Field, ...] = ()
    oauth: OAuth | None = None
    events: tuple[str, ...] = ("lead.created", "lead.status_changed")
    # The provider's writable fields, for the mapping UI.
    targets: tuple[tuple[str, str], ...] = ()
    default_mapping: dict[str, str] = field(default_factory=dict)
    # Set when the integration is configured outside the connector
    # layer (analytics ids, S3 settings) — listed for completeness so
    # the Integrations screen shows the whole picture, but managed
    # elsewhere.
    managed_elsewhere: str | None = None


# Platform-side lead fields, offered as mapping sources everywhere.
LEAD_SOURCES: tuple[tuple[str, str], ...] = (
    ("full_name", "Full name"),
    ("first_name", "First name (derived)"),
    ("last_name", "Last name (derived)"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("company", "Company"),
    ("message", "Message"),
    ("status", "Pipeline status"),
    ("source_page", "Source page"),
    ("referrer", "Referrer"),
    ("utm_source", "UTM source"),
    ("utm_medium", "UTM medium"),
    ("utm_campaign", "UTM campaign"),
    ("form", "Form name"),
    ("site", "Site name"),
    ("created_at", "Captured at"),
)

CRM_EVENTS = ("lead.created", "lead.status_changed", "form.submitted", "subscriber.created")


# =====================================================================
# CRM
# =====================================================================
ZOHO = Provider(
    key="zoho_crm",
    kind="crm",
    label="Zoho CRM",
    summary="Push leads into Zoho CRM as they arrive, and keep status in step.",
    auth="oauth2",
    docs_url="https://www.zoho.com/crm/developer/docs/api/v8/",
    config_fields=(
        Field(
            "data_center", "Data centre", "select", required=True,
            help="Zoho's API host differs per region. Using the wrong one fails "
                 "authentication with a message that does not say why.",
            options=(
                ("com", "United States (.com)"),
                ("eu", "Europe (.eu)"),
                ("in", "India (.in)"),
                ("com.au", "Australia (.com.au)"),
                ("jp", "Japan (.jp)"),
                ("ca", "Canada (.ca)"),
                ("sa", "Saudi Arabia (.sa)"),
            ),
            default="com",
        ),
        Field("module", "Module", "select", required=True,
              options=(("Leads", "Leads"), ("Contacts", "Contacts")),
              default="Leads",
              help="Which Zoho module new records are written to."),
        Field("layout", "Layout name", "text",
              help="Optional. Only needed when the module has several layouts."),
    ),
    credential_fields=(
        Field("client_id", "Client ID", "text", required=True, secret=True,
              help="From a Server-based Application in the Zoho API Console."),
        Field("client_secret", "Client secret", "password", required=True, secret=True),
    ),
    oauth=OAuth(
        authorize_url="https://accounts.zoho.{dc}/oauth/v2/auth",
        token_url="https://accounts.zoho.{dc}/oauth/v2/token",
        scopes=("ZohoCRM.modules.ALL", "ZohoCRM.settings.modules.READ"),
        # Without both of these Zoho returns an access token and no
        # refresh token, and the connector silently dies in an hour.
        extra_authorize_params=(("access_type", "offline"), ("prompt", "consent")),
        instance_from_token="api_domain",
    ),
    events=CRM_EVENTS,
    targets=(
        ("Last_Name", "Last Name (required)"),
        ("First_Name", "First Name"),
        ("Email", "Email"),
        ("Phone", "Phone"),
        ("Mobile", "Mobile"),
        ("Company", "Company (required)"),
        ("Description", "Description"),
        ("Lead_Source", "Lead Source"),
        ("Lead_Status", "Lead Status"),
        ("Website", "Website"),
        ("Designation", "Title"),
    ),
    default_mapping={
        "last_name": "Last_Name",
        "first_name": "First_Name",
        "email": "Email",
        "phone": "Phone",
        "company": "Company",
        "message": "Description",
        "utm_source": "Lead_Source",
    },
)

HUBSPOT = Provider(
    key="hubspot",
    kind="crm",
    label="HubSpot",
    summary="Create and update HubSpot contacts from leads, deduplicated by email.",
    auth="api_key",
    docs_url="https://developers.hubspot.com/docs/api/crm/contacts",
    config_fields=(
        Field("object_type", "Object", "select", required=True,
              options=(("contacts", "Contacts"), ("leads", "Leads")),
              default="contacts"),
        Field("portal_id", "Portal ID", "text",
              help="Optional. Only used to build links back to HubSpot."),
    ),
    credential_fields=(
        Field(
            "access_token", "Private app token", "password", required=True, secret=True,
            help="Settings → Integrations → Private Apps. Needs the "
                 "crm.objects.contacts.write scope. A private app token is "
                 "simpler than OAuth and does not expire.",
        ),
    ),
    events=CRM_EVENTS,
    targets=(
        ("email", "Email (dedupe key)"),
        ("firstname", "First Name"),
        ("lastname", "Last Name"),
        ("phone", "Phone"),
        ("company", "Company"),
        ("jobtitle", "Job Title"),
        ("website", "Website"),
        ("hs_lead_status", "Lead Status"),
        ("message", "Message"),
        ("hs_analytics_source", "Original Source"),
    ),
    default_mapping={
        "email": "email",
        "first_name": "firstname",
        "last_name": "lastname",
        "phone": "phone",
        "company": "company",
        "message": "message",
    },
)

SALESFORCE = Provider(
    key="salesforce",
    kind="crm",
    label="Salesforce",
    summary="Create Salesforce Leads from form submissions.",
    auth="oauth2",
    docs_url="https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
    config_fields=(
        Field("environment", "Environment", "select", required=True,
              options=(("login", "Production"), ("test", "Sandbox")),
              default="login"),
        Field("sobject", "Object", "select", required=True,
              options=(("Lead", "Lead"), ("Contact", "Contact")), default="Lead"),
        Field("api_version", "API version", "text", default="v62.0"),
    ),
    credential_fields=(
        Field("client_id", "Consumer Key", "text", required=True, secret=True),
        Field("client_secret", "Consumer Secret", "password", required=True, secret=True),
    ),
    oauth=OAuth(
        authorize_url="https://{dc}.salesforce.com/services/oauth2/authorize",
        token_url="https://{dc}.salesforce.com/services/oauth2/token",
        scopes=("api", "refresh_token", "offline_access"),
        uses_pkce=True,
        # Every org has its own API host; using login.salesforce.com for
        # data calls returns a redirect that looks like a 302 loop.
        instance_from_token="instance_url",
    ),
    events=CRM_EVENTS,
    targets=(
        ("LastName", "Last Name (required)"),
        ("FirstName", "First Name"),
        ("Company", "Company (required)"),
        ("Email", "Email"),
        ("Phone", "Phone"),
        ("Title", "Title"),
        ("Description", "Description"),
        ("LeadSource", "Lead Source"),
        ("Status", "Status"),
        ("Website", "Website"),
    ),
    default_mapping={
        "last_name": "LastName",
        "first_name": "FirstName",
        "company": "Company",
        "email": "Email",
        "phone": "Phone",
        "message": "Description",
        "utm_source": "LeadSource",
    },
)

PIPEDRIVE = Provider(
    key="pipedrive",
    kind="crm",
    label="Pipedrive",
    summary="Create Pipedrive persons and leads from form submissions.",
    auth="api_key",
    docs_url="https://developers.pipedrive.com/docs/api/v1",
    config_fields=(
        Field("company_domain", "Company domain", "text", required=True,
              help="The subdomain in your Pipedrive URL, e.g. `acme` "
                   "for acme.pipedrive.com."),
    ),
    credential_fields=(
        Field("api_token", "API token", "password", required=True, secret=True,
              help="Personal preferences → API."),
    ),
    events=CRM_EVENTS,
    targets=(
        ("name", "Name (required)"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("org_name", "Organisation"),
        ("notes", "Note"),
    ),
    default_mapping={
        "full_name": "name", "email": "email",
        "phone": "phone", "company": "org_name", "message": "notes",
    },
)


# =====================================================================
# Automation
# =====================================================================
def _automation(key: str, label: str, summary: str, docs: str, help_text: str) -> Provider:
    """Zapier, Make and n8n all receive a JSON POST at a URL they mint.

    They are separate entries rather than one "generic" because the
    setup instructions differ, and a screen that says "paste your Make
    webhook URL" is the difference between working and a support ticket.
    """
    return Provider(
        key=key,
        kind="automation",
        label=label,
        summary=summary,
        auth="api_key",
        docs_url=docs,
        config_fields=(
            Field("url", "Webhook URL", "url", required=True, help=help_text),
        ),
        credential_fields=(
            Field("secret", "Shared secret", "password", secret=True,
                  help="Optional. When set, requests carry an "
                       "x-crm-signature header over the body so the receiver "
                       "can verify they came from here."),
        ),
        # Order matters: the setup form pre-selects the first two, and
        # so does the API when none are given. Sorted alphabetically
        # that made `build.failed` the default, which is nobody's
        # reason for connecting Zapier.
        events=(
            "lead.created", "lead.status_changed", "form.submitted",
            "subscriber.created", "content.published", "campaign.sent",
            "build.failed", "health.down",
        ),
        targets=(),   # a JSON body: the whole payload goes, mapping optional
    )


ZAPIER = _automation(
    "zapier", "Zapier",
    "Trigger a Zap on any platform event.",
    "https://zapier.com/apps/webhook/integrations",
    "Create a Zap with the “Webhooks by Zapier — Catch Hook” trigger and "
    "paste the URL it gives you.",
)
MAKE = _automation(
    "make", "Make",
    "Trigger a Make scenario on any platform event.",
    "https://www.make.com/en/help/tools/webhooks",
    "Add a “Custom webhook” module to your scenario and paste its address.",
)
N8N = _automation(
    "n8n", "n8n",
    "Trigger an n8n workflow on any platform event.",
    "https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/",
    "Add a Webhook node, set it to POST, and paste its production URL.",
)


# =====================================================================
# Email
# =====================================================================
EMAIL_EVENTS = ("email.send",)

SES = Provider(
    key="ses",
    kind="email",
    label="Amazon SES",
    summary="Send transactional email and campaigns through Amazon SES.",
    auth="api_key",
    docs_url="https://docs.aws.amazon.com/ses/latest/APIReference-V2/",
    config_fields=(
        Field("region", "Region", "text", required=True, default="us-east-1",
              help="The SES region your identity is verified in."),
        Field("from_email", "From address", "text", required=True,
              help="Must be a verified identity in that region."),
        Field("from_name", "From name", "text"),
        Field("configuration_set", "Configuration set", "text",
              help="Optional. Needed for SES event publishing (bounces, "
                   "complaints) via SNS."),
    ),
    credential_fields=(
        Field("access_key_id", "Access key ID", "text", secret=True,
              help="Leave both blank on AWS to use the task's IAM role, which "
                   "is the better answer — no key to rotate or leak."),
        Field("secret_access_key", "Secret access key", "password", secret=True),
    ),
    events=EMAIL_EVENTS,
)

BREVO = Provider(
    key="brevo",
    kind="email",
    label="Brevo",
    summary="Send through Brevo's transactional email API.",
    auth="api_key",
    docs_url="https://developers.brevo.com/reference/sendtransacemail",
    config_fields=(
        Field("from_email", "From address", "text", required=True),
        Field("from_name", "From name", "text"),
    ),
    credential_fields=(
        Field("api_key", "API key", "password", required=True, secret=True,
              help="SMTP & API → API keys."),
    ),
    events=EMAIL_EVENTS,
)

SENDGRID = Provider(
    key="sendgrid",
    kind="email",
    label="SendGrid",
    summary="Send through SendGrid's v3 mail API.",
    auth="api_key",
    docs_url="https://www.twilio.com/docs/sendgrid/api-reference/mail-send",
    config_fields=(
        Field("from_email", "From address", "text", required=True,
              help="Must be a verified sender or a verified domain."),
        Field("from_name", "From name", "text"),
    ),
    credential_fields=(
        Field("api_key", "API key", "password", required=True, secret=True,
              help="Needs the Mail Send permission only."),
    ),
    events=EMAIL_EVENTS,
)

MAILCHIMP = Provider(
    key="mailchimp",
    kind="email",
    label="Mailchimp Transactional",
    summary="Send through Mailchimp Transactional (Mandrill).",
    auth="api_key",
    docs_url="https://mailchimp.com/developer/transactional/api/messages/",
    config_fields=(
        Field("from_email", "From address", "text", required=True),
        Field("from_name", "From name", "text"),
    ),
    credential_fields=(
        Field("api_key", "API key", "password", required=True, secret=True,
              help="A Mailchimp Transactional key, not a Marketing API key — "
                   "they are different products with different keys."),
    ),
    events=EMAIL_EVENTS,
)

SMTP = Provider(
    key="smtp",
    kind="email",
    label="SMTP",
    summary="Any SMTP relay. The fallback that works with every provider.",
    auth="api_key",
    config_fields=(
        Field("host", "SMTP host", "text", required=True),
        Field("port", "Port", "text", default="587",
              help="587 for STARTTLS, 465 for implicit TLS."),
        Field("from_email", "From address", "text", required=True),
        Field("from_name", "From name", "text"),
    ),
    credential_fields=(
        Field("username", "Username", "text", secret=True),
        Field("password", "Password", "password", secret=True),
    ),
    events=EMAIL_EVENTS,
)


# =====================================================================
# Analytics and storage — configured elsewhere, listed for completeness
# =====================================================================
GA4 = Provider(
    key="ga4", kind="analytics", label="Google Analytics 4",
    summary="Measurement ID and the gtag snippet for your frontend.",
    auth="none", events=(),
    managed_elsewhere="#/insights?tab=integrations",
)
GTM = Provider(
    key="gtm", kind="analytics", label="Google Tag Manager",
    summary="Container ID and the GTM snippet.",
    auth="none", events=(),
    managed_elsewhere="#/insights?tab=integrations",
)
SEARCH_CONSOLE = Provider(
    key="search_console", kind="analytics", label="Google Search Console",
    summary="Site verification token.",
    auth="none", events=(),
    managed_elsewhere="#/insights?tab=integrations",
)
S3 = Provider(
    key="s3", kind="storage", label="Object storage (S3)",
    summary="Where media is stored. Set per install, or per site.",
    auth="none", events=(),
    managed_elsewhere="#/platform",
)
CDN = Provider(
    key="cdn", kind="storage", label="CDN (CloudFront)",
    summary="Distribution invalidated after a publish.",
    auth="none", events=(),
    managed_elsewhere="#/publishing",
)


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        ZOHO, HUBSPOT, SALESFORCE, PIPEDRIVE,
        ZAPIER, MAKE, N8N,
        SES, BREVO, SENDGRID, MAILCHIMP, SMTP,
        GA4, GTM, SEARCH_CONSOLE, S3, CDN,
    )
}

# Providers you can actually create a connector row for.
CONFIGURABLE = {key: p for key, p in PROVIDERS.items() if not p.managed_elsewhere}


def get(key: str) -> Provider:
    provider = PROVIDERS.get((key or "").strip().lower())
    if provider is None:
        raise KeyError(f"unknown provider {key!r}")
    return provider


def describe(provider: Provider) -> dict:
    """JSON for the admin, so the setup form is generated not hand-built."""
    return {
        "key": provider.key,
        "kind": provider.kind,
        "label": provider.label,
        "summary": provider.summary,
        "auth": provider.auth,
        "docsUrl": provider.docs_url,
        "managedElsewhere": provider.managed_elsewhere,
        "configFields": [_field(f) for f in provider.config_fields],
        "credentialFields": [_field(f) for f in provider.credential_fields],
        "events": list(provider.events),
        "targets": [{"value": v, "label": text} for v, text in provider.targets],
        "sources": [{"value": v, "label": text} for v, text in LEAD_SOURCES],
        "defaultMapping": dict(provider.default_mapping),
        "usesOAuth": provider.auth == "oauth2",
    }


def _field(f: Field) -> dict:
    return {
        "name": f.name,
        "label": f.label,
        "type": f.type,
        "required": f.required,
        "help": f.help,
        "options": [{"value": v, "label": text} for v, text in f.options],
        "default": f.default,
        "secret": f.secret,
    }


def catalogue() -> list[dict]:
    """Every provider, grouped by kind, for the Integrations screen."""
    return [describe(p) for p in PROVIDERS.values()]
