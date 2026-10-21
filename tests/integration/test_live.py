"""Smoke tests against the live Verified API. Run with:  pytest -m integration

Needs VERIFIED_EMAIL and VERIFIED_PASSWORD (VERIFIED_COMPANY_ID optional). Builds a DRAFT
envelope (never published, so nobody is notified) and deletes it again.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest

from verified_esign import (APIStatusError, Envelope, EnvelopeSettings, RecipientCreate, Sender, SigningDocument,
                            VerifiedClient, make_tag)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("VERIFIED_EMAIL"), reason="VERIFIED_* credentials not set"),
]


def _pdf(text: bytes = b"qls-verified-esign live test - do not sign") -> bytes:
    """A minimal valid one-page PDF (with xref), the same shape the live draft test used."""
    stream = b"BT /F1 18 Tf 72 720 Td (" + text + b") Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


PDF = _pdf()


@pytest.fixture(scope="module")
def verified() -> Iterator[VerifiedClient]:
    with VerifiedClient() as client:
        yield client


def test_userinfo_and_namespace(verified: VerifiedClient) -> None:
    info = verified.userinfo()
    assert info.company_ids
    assert verified.namespace.startswith("/companies/")


def test_draft_roundtrip(verified: VerifiedClient) -> None:
    me = verified.userinfo()
    sent = verified.send_for_signing(
        SigningDocument("sdk-live-test.pdf", PDF, attachments=[("sdk-live-bilaga.pdf", PDF)]),
        [RecipientCreate(given_name=me.given_name or "Test", family_name=me.family_name or "Signer",
                         email=me.email or "test@example.com")],
        settings=EnvelopeSettings(greeting="qls-verified-esign live test - not published"),
        tags=[make_tag("SDK Live Test AB")],
        publish=False,
    )
    try:
        envelope = verified.envelopes.get(sent.envelope_id)
        assert envelope.status == "draft"
        assert [d.id for d in envelope.documents] == sent.document_ids
        assert [r.id for r in envelope.recipients] == sent.recipient_ids
        kinds = sorted(f.file_type or "" for f in verified.documents.list_files(sent.envelope_id, sent.document_ids[0]))
        assert kinds == ["attachment", "document"]
        assert verified.documents.get(sent.envelope_id, sent.document_ids[0]).tags == ["sdk-live-test-ab"]
        # Newest first, and give the asynchronously updated search index a moment.
        by_tag: list[Envelope] = []
        for _ in range(10):
            by_tag = verified.envelopes.query(
                {"field": "documents.tags", "op": "in", "value": ["sdk-live-test-ab"]},
                limit=100, sort={"created": -1})
            if sent.envelope_id in [e.id for e in by_tag]:
                break
            time.sleep(1.5)
        assert sent.envelope_id in [e.id for e in by_tag]
        # The filter must actually filter: every hit carries the tag (not just "all envelopes").
        untagged = [e.id for e in by_tag if not any("sdk-live-test-ab" in d.tags for d in e.documents)]
        assert not untagged, f"documents.tags filter returned {len(untagged)} untagged envelope(s)"
        assert any(e.id == sent.envelope_id for e in verified.envelopes.query(
            {"field": "published", "op": "eq", "value": False}, limit=100, sort={"created": -1}))
    finally:
        verified.envelopes.delete(sent.envelope_id)


def _sender_from_env() -> Sender:
    """VERIFIED_TEST_SENDER="Förnamn Efternamn <e-post>" - ideally another user of the same
    Verified company. Default: a person who is NOT a Verified user, to see whether Verified
    accepts any sender or only its own users."""
    raw = os.getenv("VERIFIED_TEST_SENDER", "SDK Avsändartest <sdk-sender-test@example.com>")
    name, _, email = raw.partition("<")
    given, _, family = name.strip().rpartition(" ")
    return Sender(email=email.strip(" >"), given_name=given or name.strip(), family_name=family or "-")


def test_sender_override(verified: VerifiedClient) -> None:
    """Does Verified honour ``sender`` on create (in its spec, not its guides)? A DRAFT only,
    deleted again - nobody is notified. The failure message says what Verified did."""
    me = verified.userinfo()
    sender = _sender_from_env()
    try:
        created = verified.envelopes.create(sender=sender)
    except APIStatusError as exc:
        pytest.fail(f"Verified REFUSED the sender {sender.email}: HTTP {exc.status_code} {exc.body!r}")
    try:
        envelope = verified.envelopes.get(created.id)
        got = envelope.sender.email if envelope.sender else None
        print(f"\nsender requested: {sender.email}; Verified recorded: {got} "
              f"({envelope.sender.given_name if envelope.sender else None} "
              f"{envelope.sender.family_name if envelope.sender else None}); API user: {me.email}")
        assert got is not None and got.lower() == sender.email.lower(), (
            f"Verified IGNORED the sender: it recorded {got!r} (the API user is {me.email!r})")
    finally:
        verified.envelopes.delete(created.id)
