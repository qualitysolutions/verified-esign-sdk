"""Verified webhooks: AWS SNS message verification and event parsing.

Verified delivers webhooks through **AWS SNS**. There is no shared secret: authenticity
comes from SNS's RSA signature over the message, checked against the X.509 certificate at
``SigningCertURL`` (which must be an ``sns.<region>.amazonaws.com`` HTTPS URL, or anyone
could sign with their own certificate). Webhooks are registered per company in the
Verified web app (Company settings > Webhooks), not via the API, and each company needs
its own endpoint URL.

A receiver does three things::

    verifier = WebhookVerifier(expected_namespace="/companies/HyTjgHzt4",
                               allowed_topic_arns={"arn:aws:sns:eu-north-1:...:WebhooksAdmin-..."})
    msg = verifier.verify(request_body)          # raises WebhookVerificationError
    if msg.type == "SubscriptionConfirmation":
        verifier.confirm_subscription(msg)       # GET SubscribeURL - required once
    elif msg.event and msg.event.event_type == EVENT_SIGNED:
        ...  # enqueue: fetch the envelope + signed PDF via the API

The event carries no envelope data, events may arrive late or out of order, and only a
subset of state changes emit one: always re-read the envelope through the API and treat
events as "something changed" hints. Needs the ``webhooks`` extra
(``pip install qls-verified-esign[webhooks]``) for the ``cryptography`` dependency.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError

from .errors import WebhookVerificationError
from .models.common import ReadModel

#: Every envelope was signed by all recipients and the sealed (signed) PDF is attached.
EVENT_SIGNED = "sealedDocument.attached"
EVENT_PUBLISHED = "envelope.published"
EVENT_TRASHED = "envelope.trashed"
EVENT_ABORTED = "envelope.aborted"
EVENT_EXPIRATION_SET = "envelope.expiration.set"
EVENT_DOCUMENT_ABORTED = "document.aborted"
EVENT_DOCUMENT_REJECTED = "document.rejected"
EVENT_NOTIFICATION_BOUNCED = "notification.bounced"
#: The event types Verified documents (2026-10). There is no per-recipient "signed" event.
EVENT_TYPES = frozenset({
    EVENT_SIGNED, EVENT_PUBLISHED, EVENT_TRASHED, EVENT_ABORTED, EVENT_EXPIRATION_SET,
    EVENT_DOCUMENT_ABORTED, EVENT_DOCUMENT_REJECTED, EVENT_NOTIFICATION_BOUNCED,
})

MessageType = Literal["Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"]

_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_SIGNED_KEYS: dict[str, tuple[str, ...]] = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}
#: Bodies larger than this are rejected before parsing (SNS caps messages at 256 KB).
MAX_BODY_BYTES = 256 * 1024


class WebhookResource(ReadModel):
    """``resource`` of a Verified event. Keys arrive in MIXED case (``id``/``uid`` vs
    ``Version``/``Namespace``); both spellings are accepted."""

    type: str | None = None
    id: str
    uid: str | None = None
    version: int | None = Field(default=None, validation_alias="Version")
    namespace: str | None = Field(default=None, validation_alias="Namespace")
    flow_id: str | None = None
    flow_config_id: str | None = None

    @property
    def envelope_id(self) -> str | None:
        """The envelope this event is about (``resource.type == "envelope"``)."""
        if self.type in (None, "envelope"):
            return self.id
        match = re.match(r"^/envelopes/([^/]+)", self.uid or "")
        return match.group(1) if match else None


class WebhookEvent(ReadModel):
    """The Verified event inside an SNS ``Notification`` (its ``Message``, JSON-in-JSON)."""

    event_type: str = Field(validation_alias="EventType")
    created: datetime | None = Field(default=None, validation_alias="Created")
    resource: WebhookResource

    @property
    def is_known_type(self) -> bool:
        return self.event_type in EVENT_TYPES


class WebhookMessage(ReadModel):
    """A verified SNS message."""

    type: MessageType = Field(validation_alias="Type")
    message_id: str = Field(validation_alias="MessageId")
    topic_arn: str = Field(validation_alias="TopicArn")
    timestamp: datetime = Field(validation_alias="Timestamp")
    message: str = Field(validation_alias="Message")
    subscribe_url: str | None = Field(default=None, validation_alias="SubscribeURL")
    #: Set for ``Notification`` messages whose ``Message`` is a Verified event.
    event: WebhookEvent | None = None


def _is_sns_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(_SNS_HOST.match(parts.hostname or "")) and parts.port in (None, 443)


def string_to_sign(raw: Mapping[str, Any]) -> bytes:
    """The canonical SNS string-to-sign: ``Key\\nValue\\n`` for the signed keys present."""
    keys = _SIGNED_KEYS.get(str(raw.get("Type")))
    if keys is None:
        raise WebhookVerificationError(f"Unsupported SNS message type {raw.get('Type')!r}")
    return "".join(f"{k}\n{raw[k]}\n" for k in keys if raw.get(k) is not None).encode()


class WebhookVerifier:
    """Verifies SNS signatures and parses Verified events. Thread-safe; reuse one instance
    so downloaded signing certificates are cached.

    ``expected_namespace`` (``/companies/<id>``) rejects events for another company.
    ``allowed_topic_arns`` pins the SNS topic(s) Verified publishes from; recommended in
    production (log the ARN from the first confirmed subscription and pin it).
    ``max_age`` rejects messages whose ``Timestamp`` is older (replay protection).
    """

    def __init__(
        self,
        *,
        expected_namespace: str | None = None,
        allowed_topic_arns: Collection[str] | None = None,
        max_age: float = 3600.0,
        http_client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._namespace = expected_namespace
        self._topics = frozenset(allowed_topic_arns) if allowed_topic_arns else None
        self._max_age = max_age
        self._client = http_client
        self._timeout = timeout
        self._certs: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- parsing ------------------------------------------------------------------------
    def parse_unverified(self, body: bytes | str) -> dict[str, Any]:
        """Decode the SNS JSON body (sent as ``text/plain``) without checking anything."""
        raw_bytes = body.encode() if isinstance(body, str) else body
        if len(raw_bytes) > MAX_BODY_BYTES:
            raise WebhookVerificationError("Webhook body too large")
        try:
            raw = json.loads(raw_bytes)
        except ValueError as exc:
            raise WebhookVerificationError("Webhook body is not JSON") from exc
        if not isinstance(raw, dict):
            raise WebhookVerificationError("Webhook body is not a JSON object")
        return raw

    def _check_claims(self, raw: Mapping[str, Any], now: float | None) -> None:
        for key in ("Type", "MessageId", "TopicArn", "Timestamp", "Message", "Signature",
                    "SignatureVersion", "SigningCertURL"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise WebhookVerificationError(f"SNS message lacks {key}")
        if raw["SignatureVersion"] not in ("1", "2"):
            raise WebhookVerificationError(f"Unsupported SignatureVersion {raw['SignatureVersion']!r}")
        if not _is_sns_url(raw["SigningCertURL"]) or not urlsplit(raw["SigningCertURL"]).path.endswith(".pem"):
            raise WebhookVerificationError("SigningCertURL is not an AWS SNS certificate URL")
        if self._topics is not None and raw["TopicArn"] not in self._topics:
            raise WebhookVerificationError("Unexpected TopicArn")
        try:
            sent = datetime.fromisoformat(str(raw["Timestamp"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise WebhookVerificationError("Invalid Timestamp") from exc
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        age = (now if now is not None else time.time()) - sent.timestamp()
        if age > self._max_age or age < -300:
            raise WebhookVerificationError(f"Message timestamp outside the accepted window (age {age:.0f}s)")

    def _verify_signature(self, raw: Mapping[str, Any], cert_pem: bytes) -> None:
        try:
            from cryptography import x509
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise WebhookVerificationError(
                "Webhook verification needs the 'webhooks' extra: pip install qls-verified-esign[webhooks]"
            ) from exc
        cert = self._certs.get(raw["SigningCertURL"])
        if cert is None:
            cert = x509.load_pem_x509_certificate(cert_pem)
            with self._lock:
                self._certs[raw["SigningCertURL"]] = cert
        key = cert.public_key()
        if not isinstance(key, rsa.RSAPublicKey):
            raise WebhookVerificationError("SNS certificate does not hold an RSA key")
        algorithm = hashes.SHA1() if raw["SignatureVersion"] == "1" else hashes.SHA256()  # noqa: S303 - SNS v1
        try:
            signature = base64.b64decode(raw["Signature"], validate=True)
            key.verify(signature, string_to_sign(raw), padding.PKCS1v15(), algorithm)
        except (InvalidSignature, ValueError) as exc:
            raise WebhookVerificationError("SNS signature is invalid") from exc

    def _build(self, raw: Mapping[str, Any]) -> WebhookMessage:
        try:
            msg = WebhookMessage.model_validate(raw)
        except ValidationError as exc:
            raise WebhookVerificationError(f"Malformed SNS message: {exc.error_count()} error(s)") from exc
        if msg.type == "Notification":
            try:
                event = WebhookEvent.model_validate(json.loads(msg.message))
            except (ValueError, ValidationError) as exc:
                raise WebhookVerificationError("Notification Message is not a Verified event") from exc
            if self._namespace and event.resource.namespace != self._namespace:
                raise WebhookVerificationError("Event is for another company namespace")
            msg = msg.model_copy(update={"event": event})
        elif msg.subscribe_url is None or not _is_sns_url(msg.subscribe_url):
            raise WebhookVerificationError("SubscribeURL is not an AWS SNS URL")
        return msg

    # -- public API ---------------------------------------------------------------------
    def verify(self, body: bytes | str, *, now: float | None = None) -> WebhookMessage:
        """Verify an incoming SNS POST body and return the parsed message, or raise
        :class:`WebhookVerificationError` (answer the request with 400/403 then)."""
        raw = self.parse_unverified(body)
        self._check_claims(raw, now)
        url = raw["SigningCertURL"]
        pem = b"" if url in self._certs else self._fetch(url)
        self._verify_signature(raw, pem)
        return self._build(raw)

    async def averify(self, body: bytes | str, *, http_client: httpx.AsyncClient | None = None,
                      now: float | None = None) -> WebhookMessage:
        """Async :meth:`verify`; the certificate is fetched with ``http_client`` (or a
        short-lived client) the first time it is seen."""
        raw = self.parse_unverified(body)
        self._check_claims(raw, now)
        url = raw["SigningCertURL"]
        pem = b"" if url in self._certs else await self._afetch(url, http_client)
        self._verify_signature(raw, pem)
        return self._build(raw)

    def confirm_subscription(self, message: WebhookMessage) -> None:
        """Confirm a ``SubscriptionConfirmation`` by GETting its ``SubscribeURL``.
        Until this happens the webhook stays pending; if it never happens Verified disables
        it, and a disabled webhook must be deleted and re-created."""
        self._fetch(self._subscribe_url(message))

    async def aconfirm_subscription(self, message: WebhookMessage,
                                    *, http_client: httpx.AsyncClient | None = None) -> None:
        await self._afetch(self._subscribe_url(message), http_client)

    # -- HTTP ---------------------------------------------------------------------------
    @staticmethod
    def _subscribe_url(message: WebhookMessage) -> str:
        if message.type != "SubscriptionConfirmation" or not message.subscribe_url:
            raise WebhookVerificationError("Not a SubscriptionConfirmation message")
        if not _is_sns_url(message.subscribe_url):
            raise WebhookVerificationError("SubscribeURL is not an AWS SNS URL")
        return message.subscribe_url

    def _fetch(self, url: str) -> bytes:
        try:
            if self._client is not None:
                response = self._client.get(url, timeout=self._timeout)
            else:
                response = httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise WebhookVerificationError(f"Could not reach AWS SNS: {exc}") from exc
        if response.status_code != 200:
            raise WebhookVerificationError(f"AWS SNS returned HTTP {response.status_code}")
        return response.content

    async def _afetch(self, url: str, client: httpx.AsyncClient | None) -> bytes:
        try:
            if client is not None:
                response = await client.get(url, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as tmp:
                    response = await tmp.get(url)
        except httpx.HTTPError as exc:
            raise WebhookVerificationError(f"Could not reach AWS SNS: {exc}") from exc
        if response.status_code != 200:
            raise WebhookVerificationError(f"AWS SNS returned HTTP {response.status_code}")
        return response.content
