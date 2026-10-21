"""Smoke tests against the live Verified API. Run with:  pytest -m integration

Needs VERIFIED_EMAIL and VERIFIED_PASSWORD (VERIFIED_COMPANY_ID optional). Builds a DRAFT
envelope (never published, so nobody is notified) and deletes it again.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from verified_esign import EnvelopeSettings, RecipientCreate, SigningDocument, VerifiedClient, make_tag

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
        by_tag = verified.envelopes.query(
            {"field": "documents.tags", "op": "in", "value": ["sdk-live-test-ab"]}, limit=100)
        assert sent.envelope_id in [e.id for e in by_tag]
        assert any(e.id == sent.envelope_id for e in verified.envelopes.query(
            {"field": "published", "op": "eq", "value": False}, limit=100, sort={"created": -1}))
    finally:
        verified.envelopes.delete(sent.envelope_id)
