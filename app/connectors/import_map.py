"""Provider key → implementation.

Its own module purely to break the import cycle: base.py needs to look
a class up, and every provider class inherits from base.Connector.
"""

from __future__ import annotations

from .automation import AutomationConnector
from .crm import HubSpotConnector, PipedriveConnector, SalesforceConnector, ZohoConnector
from .email import (
    BrevoConnector,
    MailchimpConnector,
    SendGridConnector,
    SesConnector,
    SmtpConnector,
)

CONNECTOR_CLASSES = {
    "zoho_crm": ZohoConnector,
    "hubspot": HubSpotConnector,
    "salesforce": SalesforceConnector,
    "pipedrive": PipedriveConnector,
    "zapier": AutomationConnector,
    "make": AutomationConnector,
    "n8n": AutomationConnector,
    "ses": SesConnector,
    "brevo": BrevoConnector,
    "sendgrid": SendGridConnector,
    "mailchimp": MailchimpConnector,
    "smtp": SmtpConnector,
}
