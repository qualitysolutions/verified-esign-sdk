"""Python SDK for the Verified (verified.eu) eSign API.

Quick start::

    from verified_esign import VerifiedClient, RecipientCreate, SigningDocument

    with VerifiedClient() as verified:          # reads VERIFIED_* environment variables
        sent = verified.send_for_signing(
            SigningDocument("Avtal AVT-2026-0042.pdf", "avtal.pdf",
                            attachments=[("Bilaga 1 - Tjänstebeskrivning.pdf", "bilaga1.pdf")]),
            [RecipientCreate(given_name="Anna", family_name="Andersson", email="anna@kund.se")],
        )
        print(sent.envelope_id)

See ``README.md`` for authentication, the IP lock, webhooks and downloading signed PDFs.
"""

from __future__ import annotations

import logging

from ._transport import Response
from .client import AsyncVerifiedClient, SentEnvelope, SigningDocument, VerifiedClient
from .config import DEFAULT_BASE_URL, Config
from .errors import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
    UnprocessableEntityError,
    VerifiedError,
    WebhookVerificationError,
)
from .models import (
    Created,
    Document,
    DocumentCreate,
    Envelope,
    EnvelopeSettings,
    FileRef,
    Person,
    Recipient,
    RecipientCreate,
    RecipientRole,
    RecipientUpdate,
    Role,
    Sender,
    SigningMethod,
    UserInfo,
    make_tag,
    validate_tag,
)
from .webhooks import (
    EVENT_SIGNED,
    EVENT_TYPES,
    WebhookEvent,
    WebhookMessage,
    WebhookResource,
    WebhookVerifier,
)

__version__ = "2026.10.21.2"

# Silent unless the host app configures a handler. The SDK logs to "verified_esign" at
# DEBUG (method, path, status, timing) and WARNING (retries), and never logs credentials,
# tokens, signing links, headers or request/response bodies.
logging.getLogger("verified_esign").addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    "VerifiedClient", "AsyncVerifiedClient", "Config", "DEFAULT_BASE_URL", "Response",
    "SentEnvelope", "SigningDocument",
    # models
    "Created", "Document", "DocumentCreate", "Envelope", "EnvelopeSettings", "FileRef", "Person",
    "Recipient", "RecipientCreate", "RecipientRole", "RecipientUpdate", "Role", "Sender", "SigningMethod", "UserInfo",
    "make_tag", "validate_tag",
    # webhooks
    "WebhookVerifier", "WebhookMessage", "WebhookEvent", "WebhookResource", "EVENT_SIGNED", "EVENT_TYPES",
    # errors
    "VerifiedError", "ConfigurationError", "AuthenticationError", "WebhookVerificationError",
    "APIError", "APIConnectionError", "APITimeoutError", "APIStatusError",
    "BadRequestError", "UnauthorizedError", "ForbiddenError", "NotFoundError", "ConflictError",
    "UnprocessableEntityError", "RateLimitError", "ServerError",
]
