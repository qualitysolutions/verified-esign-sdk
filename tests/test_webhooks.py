"""SNS signature verification and Verified event parsing, with a locally minted certificate."""

from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from verified_esign import EVENT_SIGNED, WebhookVerificationError, WebhookVerifier
from verified_esign.webhooks import string_to_sign

CERT_URL = "https://sns.eu-north-1.amazonaws.com/SimpleNotificationService-0123456789abcdef.pem"
TOPIC = "arn:aws:sns:eu-north-1:943712263485:WebhooksAdmin-SNSTopic-TEST"
NAMESPACE = "/companies/COMPANY1"
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
_CERT = (x509.CertificateBuilder().subject_name(_NAME).issuer_name(_NAME).public_key(_KEY.public_key())
         .serial_number(1).not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=1))
         .sign(_KEY, hashes.SHA256()))
CERT_PEM = _CERT.public_bytes(serialization.Encoding.PEM)


def sign(msg: dict[str, Any], version: str = "1") -> dict[str, Any]:
    msg = {**msg, "SignatureVersion": version, "SigningCertURL": CERT_URL}
    algo = hashes.SHA1() if version == "1" else hashes.SHA256()
    msg["Signature"] = base64.b64encode(_KEY.sign(string_to_sign(msg), padding.PKCS1v15(), algo)).decode()
    return msg


def notification(event_type: str = EVENT_SIGNED, namespace: str = NAMESPACE, **over: Any) -> dict[str, Any]:
    event = {"Created": "2026-10-21T11:59:58.000Z", "EventType": event_type, "resource": {
        "type": "envelope", "id": "SJniAcPou", "uid": "/envelopes/SJniAcPou", "Version": 56441,
        "Namespace": namespace, "flowId": "default", "flowConfigId": "default"}}
    msg = {"Type": "Notification", "MessageId": "7ed14748-3f0d-5570-8994-1e0c1ffa30ab", "TopicArn": TOPIC,
           "Message": json.dumps(event), "Timestamp": "2026-10-21T11:59:59.299Z",
           "UnsubscribeURL": "https://sns.eu-north-1.amazonaws.com/?Action=Unsubscribe&SubscriptionArn=x",
           "MessageAttributes": {"namespace": {"Type": "String", "Value": namespace}}}
    msg.update(over)
    return msg


def confirmation() -> dict[str, Any]:
    return {"Type": "SubscriptionConfirmation", "MessageId": "m-1", "Token": "tok", "TopicArn": TOPIC,
            "Message": "You have chosen to subscribe to the topic ...",
            "SubscribeURL": "https://sns.eu-north-1.amazonaws.com/?Action=ConfirmSubscription&Token=tok",
            "Timestamp": "2026-10-21T11:59:59.000Z"}


@pytest.fixture
def verifier(respx_mock: Any) -> WebhookVerifier:
    respx_mock.get(CERT_URL).mock(return_value=httpx.Response(200, content=CERT_PEM))
    return WebhookVerifier(expected_namespace=NAMESPACE, allowed_topic_arns={TOPIC})


def body(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg).encode()


@pytest.mark.parametrize("version", ["1", "2"])
def test_valid_notification(verifier: WebhookVerifier, version: str) -> None:
    msg = verifier.verify(body(sign(notification(), version)), now=NOW.timestamp())
    assert msg.type == "Notification" and msg.event is not None
    assert msg.event.event_type == EVENT_SIGNED and msg.event.is_known_type
    assert msg.event.resource.envelope_id == "SJniAcPou"
    assert msg.event.resource.namespace == NAMESPACE and msg.event.resource.version == 56441


def test_certificate_is_cached(verifier: WebhookVerifier, respx_mock: Any) -> None:
    for _ in range(3):
        verifier.verify(body(sign(notification())), now=NOW.timestamp())
    assert respx_mock.routes[0].call_count == 1


def test_tampered_message_rejected(verifier: WebhookVerifier) -> None:
    msg = sign(notification())
    msg["Message"] = msg["Message"].replace("SJniAcPou", "OTHERENV1")
    with pytest.raises(WebhookVerificationError, match="signature"):
        verifier.verify(body(msg), now=NOW.timestamp())


@pytest.mark.parametrize("url", [
    "https://evil.example.com/cert.pem",
    "http://sns.eu-north-1.amazonaws.com/cert.pem",
    "https://sns.eu-north-1.amazonaws.com.evil.com/cert.pem",
    "https://sns.eu-north-1.amazonaws.com/cert.txt",
])
def test_foreign_certificate_url_rejected(verifier: WebhookVerifier, url: str) -> None:
    msg = sign(notification())
    msg["SigningCertURL"] = url
    with pytest.raises(WebhookVerificationError, match="SigningCertURL"):
        verifier.verify(body(msg), now=NOW.timestamp())


def test_other_company_rejected(verifier: WebhookVerifier) -> None:
    with pytest.raises(WebhookVerificationError, match="another company"):
        verifier.verify(body(sign(notification(namespace="/companies/OTHER"))), now=NOW.timestamp())


def test_unexpected_topic_rejected(verifier: WebhookVerifier) -> None:
    with pytest.raises(WebhookVerificationError, match="TopicArn"):
        verifier.verify(body(sign(notification(TopicArn="arn:aws:sns:eu-north-1:1:other"))), now=NOW.timestamp())


def test_stale_message_rejected(verifier: WebhookVerifier) -> None:
    later = (NOW + dt.timedelta(hours=2)).timestamp()
    with pytest.raises(WebhookVerificationError, match="window"):
        verifier.verify(body(sign(notification())), now=later)


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", b"x" * (300 * 1024)])
def test_garbage_rejected(verifier: WebhookVerifier, raw: bytes) -> None:
    with pytest.raises(WebhookVerificationError):
        verifier.verify(raw, now=NOW.timestamp())


def test_subscription_confirmation(verifier: WebhookVerifier, respx_mock: Any) -> None:
    confirm = respx_mock.get("https://sns.eu-north-1.amazonaws.com/").mock(return_value=httpx.Response(200))
    msg = verifier.verify(body(sign(confirmation())), now=NOW.timestamp())
    assert msg.type == "SubscriptionConfirmation" and msg.event is None
    verifier.confirm_subscription(msg)
    assert confirm.called


def test_confirm_refuses_notifications(verifier: WebhookVerifier) -> None:
    msg = verifier.verify(body(sign(notification())), now=NOW.timestamp())
    with pytest.raises(WebhookVerificationError):
        verifier.confirm_subscription(msg)


def test_unknown_event_type_still_parses(verifier: WebhookVerifier) -> None:
    msg = verifier.verify(body(sign(notification(event_type="envelope.something.new"))), now=NOW.timestamp())
    assert msg.event is not None and not msg.event.is_known_type


async def test_async_verify(respx_mock: Any) -> None:
    respx_mock.get(CERT_URL).mock(return_value=httpx.Response(200, content=CERT_PEM))
    v = WebhookVerifier(expected_namespace=NAMESPACE)
    msg = await v.averify(body(sign(notification())), now=NOW.timestamp())
    assert msg.event is not None and msg.event.event_type == EVENT_SIGNED
