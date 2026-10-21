"""Envelope / document / recipient calls, parsed from sanitized LIVE responses."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from verified_esign import (
    EnvelopeSettings,
    RecipientCreate,
    RecipientUpdate,
    VerifiedClient,
    VerifiedError,
)

from .conftest import BASE, PDF, live, live_response

ENV = "BybGMzXL2zl"
DOC = "BylfMfQL2fg"


def test_create_envelope_reads_location(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.post(f"{BASE}/envelope-descriptors/default/envelopes").mock(
        return_value=live_response("post_envelope-descriptors_default_envelopes.json"))
    created = client.envelopes.create()
    assert created.id == ENV and created.uid == f"/envelopes/{ENV}"
    assert json.loads(route.calls.last.request.content) == {}


def test_get_envelope_parses_live_shape(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.get(f"{BASE}/envelopes/{ENV}").mock(return_value=live_response("get_envelopes_ID.json"))
    env = client.envelopes.get(ENV)
    assert env.status == "draft" and env.published is False
    assert env.greeting == "QLS API draft test - not published"
    assert env.expiration == datetime(2026, 10, 28, 23, 59, 59, 999000, tzinfo=timezone.utc)
    assert env.sequential_signing is True and env.automatic_reminders == 0
    assert env.documents[0].id == DOC and env.documents[0].signed is False
    r = env.recipients[0]
    assert r.signing_method == "bankid-se" and r.role_name == "signer" and r.order == 1
    assert "display" in (r.model_extra or {})  # unknown fields are kept


def test_update_settings_sends_only_set_fields(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.put(f"{BASE}/envelopes/{ENV}").mock(return_value=live_response("put_envelopes_ID.json"))
    client.envelopes.update(ENV, EnvelopeSettings(greeting="Hej", automatic_reminders=0))
    assert route.calls.last.response.status_code == 202
    assert json.loads(route.calls.last.request.content) == {"greeting": "Hej", "automaticReminders": 0}


def test_publish_abort_trash_delete(client: VerifiedClient, respx_mock: Any) -> None:
    pub = respx_mock.put(f"{BASE}/envelopes/{ENV}/publish-status").mock(return_value=httpx.Response(202))
    abort = respx_mock.put(f"{BASE}/envelopes/{ENV}/abort-status").mock(return_value=httpx.Response(202))
    trash = respx_mock.put(f"{BASE}/envelopes/{ENV}/trash-status").mock(return_value=httpx.Response(202))
    delete = respx_mock.delete(f"{BASE}/envelopes/{ENV}").mock(return_value=live_response("delete_envelopes_ID.json"))
    client.envelopes.publish(ENV)
    client.envelopes.abort(ENV, comment="Fel version")
    client.envelopes.trash(ENV)
    client.envelopes.delete(ENV)
    assert json.loads(pub.calls.last.request.content) == {"published": True}
    assert json.loads(abort.calls.last.request.content) == {"comment": "Fel version"}
    assert json.loads(trash.calls.last.request.content) == {}
    assert delete.called


def test_ids_are_path_quoted(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.get(f"{BASE}/envelopes/a%2F..%2Fb").mock(return_value=httpx.Response(200, json={"id": "x"}))
    client.envelopes.get("a/../b")
    assert route.called


def test_create_document_id_only_in_location(client: VerifiedClient, respx_mock: Any) -> None:
    rec = live("post_envelopes_ID_documents.json")
    assert rec["body"] is None and rec["status"] == 200  # live: 200, not 201, and no body
    route = respx_mock.post(f"{BASE}/envelopes/{ENV}/documents").mock(
        return_value=live_response("post_envelopes_ID_documents.json"))
    from verified_esign import DocumentCreate
    created = client.documents.create(ENV, DocumentCreate(name="Avtal.pdf", tags=["naas-avtal"]))
    assert created.id == DOC
    assert json.loads(route.calls.last.request.content) == {
        "name": "Avtal.pdf", "source": "file", "descriptor": {"hash": 1}, "tags": ["naas-avtal"]}


def test_upload_is_multipart_with_name(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.post(f"{BASE}/envelopes/{ENV}/documents/{DOC}/files/document").mock(
        return_value=live_response("post_envelopes_ID_documents_ID_files_ID.json"))
    file_id = client.documents.upload(ENV, DOC, PDF, name="Avtal.pdf")
    assert file_id == live("post_envelopes_ID_documents_ID_files_ID.json")["body"]["id"]
    req = route.calls.last.request
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"; filename="Avtal.pdf"' in req.content and PDF in req.content
    assert b'name="name"' in req.content


@pytest.mark.parametrize("content,name,file_type,match", [
    (b"not a pdf", "a.pdf", "document", "PDF"),
    (PDF, "a.docx", "document", "PDF"),
    (b"x" * (10 * 1024 * 1024 + 1), "a.pdf", "document", "PDF"),
    (b"x", "a.exe", "attachment", "not accepted"),
])
def test_upload_validated_locally(client: VerifiedClient, content: bytes, name: str, file_type: str,
                                  match: str) -> None:
    with pytest.raises(ValueError, match=match):
        client.documents.upload(ENV, DOC, content, name=name, file_type=file_type)


def test_list_files(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.get(f"{BASE}/envelopes/{ENV}/documents/{DOC}/files").mock(
        return_value=live_response("get_envelopes_ID_documents_ID_files.json"))
    files = client.documents.list_files(ENV, DOC)
    assert len(files) == 1 and files[0].file_type == "document" and files[0].file_id


def test_download_signed_uses_presigned_url_without_auth(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.get(f"{BASE}/envelopes/{ENV}/documents/{DOC}").mock(return_value=httpx.Response(200, json={
        "id": DOC, "signed": True, "signedFile": {"uid": f"/envelopes/{ENV}/documents/{DOC}/files/F9"}}))
    url_route = respx_mock.get(f"{BASE}/envelopes/{ENV}/documents/{DOC}/files/F9/url").mock(
        return_value=httpx.Response(200, json={"url": "https://s3.example.com/signed.pdf?X-Amz-Signature=abc"}))
    s3 = respx_mock.get("https://s3.example.com/signed.pdf").mock(return_value=httpx.Response(200, content=PDF))
    assert client.documents.download_signed(ENV, DOC) == PDF
    assert parse_qs(urlsplit(str(url_route.calls.last.request.url)).query) == {"asObject": ["true"]}
    assert "authorization" not in s3.calls.last.request.headers


def test_download_signed_before_completion(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.get(f"{BASE}/envelopes/{ENV}/documents/{DOC}").mock(
        return_value=httpx.Response(200, json={"id": DOC, "signed": False}))
    with pytest.raises(VerifiedError, match="no signed file yet"):
        client.documents.download_signed(ENV, DOC)


def test_add_recipient(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.post(f"{BASE}/envelopes/{ENV}/recipients").mock(
        return_value=live_response("post_envelopes_ID_recipients.json"))
    created = client.recipients.add(ENV, RecipientCreate(given_name="Anna", family_name="Andersson",
                                                          email="anna@kund.se", order=1))
    assert created.id == "BJ-QMz7UhGe"
    assert json.loads(route.calls.last.request.content) == {
        "givenName": "Anna", "familyName": "Andersson", "email": "anna@kund.se", "order": 1,
        "signingMethod": "bankid-se", "language": "sv_SE", "role": {"name": "signer", "action": "sign"}}


def test_recipient_defaults_are_sent(client: VerifiedClient, respx_mock: Any) -> None:
    """Defaults (bankid-se, sv_SE, signer) must reach the API even though they are not 'set'."""
    route = respx_mock.post(f"{BASE}/envelopes/{ENV}/recipients").mock(
        return_value=live_response("post_envelopes_ID_recipients.json"))
    client.recipients.add(ENV, RecipientCreate(given_name="A", family_name="B", email="a@b.se"))
    body = json.loads(route.calls.last.request.content)
    assert body["signingMethod"] == "bankid-se" and body["language"] == "sv_SE"
    assert body["role"] == {"name": "signer", "action": "sign"}


def test_recipient_language_pattern() -> None:
    with pytest.raises(ValueError):
        RecipientCreate(given_name="A", family_name="B", email="a@b.se", language="sv")


def test_update_recipient(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.put(f"{BASE}/envelopes/{ENV}/recipients/R1").mock(return_value=httpx.Response(202))
    client.recipients.update(ENV, "R1", RecipientUpdate(email="ny@kund.se"))
    assert json.loads(route.calls.last.request.content) == {"email": "ny@kund.se"}


def test_sign_link(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.post(f"{BASE}/envelopes/{ENV}/jobs/get.sign.link").mock(return_value=httpx.Response(
        200, json={"getSignLink": {"recipient": {"R1": {"url": "https://app.verified.eu/#/sign/x?access_token=t"}}}}))
    url = client.recipients.sign_link(ENV, "R1", redirect_to="https://qarp.qls.se/klart")
    assert url.startswith("https://app.verified.eu/#/sign/")
    assert json.loads(route.calls.last.request.content) == {
        "recipient": {"id": "R1"}, "redirectTo": "https://qarp.qls.se/klart"}


def test_sign_link_missing(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.post(f"{BASE}/envelopes/{ENV}/jobs/get.sign.link").mock(
        return_value=httpx.Response(200, json={"getSignLink": {"recipient": {}}}))
    with pytest.raises(VerifiedError, match="No signing link"):
        client.recipients.sign_link(ENV, "R1")


def test_remind(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.post(f"{BASE}/envelopes/{ENV}/jobs/send.notification").mock(
        return_value=httpx.Response(202, json=None))
    client.recipients.remind(ENV, "R1", greeting="Påminnelse")
    assert json.loads(route.calls.last.request.content) == {
        "recipient": {"id": "R1"}, "envelope": {"greeting": "Påminnelse"}}


def test_query_and_iter(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.get(f"{BASE}/query-new/envelopes").mock(side_effect=[
        httpx.Response(200, json=[{"id": "A"}, {"id": "B"}]), httpx.Response(200, json=[{"id": "C"}])])
    flt = {"and": [{"field": "completed", "op": "eq", "value": True}]}
    ids = [e.id for e in client.envelopes.iter(flt, page_size=2, sort={"modified": -1})]
    assert ids == ["A", "B", "C"]
    first, second = (parse_qs(urlsplit(str(c.request.url)).query) for c in route.calls)
    assert json.loads(first["filters"][0]) == flt and first["sort"] == ['{"modified":-1}']
    assert first["from"] == ["0"] and second["from"] == ["2"] and first["limit"] == ["2"]


def test_query_shape_differs_from_get(client: VerifiedClient, respx_mock: Any) -> None:
    """/query-new returns bare ids where GET /envelopes/{id} embeds objects (live 2026-10-21:
    "descriptor": "default"), and notifications keyed by id instead of a list."""
    respx_mock.get(f"{BASE}/query-new/envelopes").mock(return_value=httpx.Response(200, json=[{
        "id": ENV, "descriptor": "default", "flow": "/flows/default", "published": True,
        "documents": [{"id": DOC, "descriptor": "1", "signed": True,
                       "signedFile": f"/envelopes/{ENV}/documents/{DOC}/files/F9"}],
        "recipients": [{"id": "R1", "role": "signer", "order": 100.5, "notifications": {
            "N1": {"id": "N1", "date": "2026-10-21T12:00:00Z", "status": "delivered"}}}],
    }]))
    env = client.envelopes.query(limit=1)[0]
    assert env.descriptor == "default" and env.status == "pending"
    assert env.documents[0].signed_file is not None and env.documents[0].signed_file.file_id == "F9"
    r = env.recipients[0]
    assert r.order == 100.5  # live: fractional order from web-app reordering
    assert r.role_name == "signer" and r.notifications == [
        {"id": "N1", "date": "2026-10-21T12:00:00Z", "status": "delivered"}]


@pytest.mark.parametrize("name,tag", [
    ("Bygg Dialog AB", "bygg-dialog-ab"),
    ("Ölands Åkeri & Bär AB", "olands-akeri-bar-ab"),
    ("  QLS quality solutions AB ", "qls-quality-solutions-ab"),
    ("Café Måne", "cafe-mane"),
    ("H&M", "h-m"),
    ("556000-0000", "556000-0000"),
])
def test_make_tag(name: str, tag: str) -> None:
    from verified_esign import make_tag
    assert make_tag(name) == tag


@pytest.mark.parametrize("bad", ["AB", "ab", "Bygg Dialog", "bygg dialog", "-abc", "abc-", "åkeri"])
def test_invalid_tags_rejected_locally(bad: str) -> None:
    from verified_esign import DocumentCreate, make_tag
    with pytest.raises(ValueError):
        DocumentCreate(name="a.pdf", tags=[bad])
    if bad in ("AB", "ab"):
        with pytest.raises(ValueError, match="at least 3"):
            make_tag(bad)


def test_duplicate_tags_collapsed() -> None:
    from verified_esign import DocumentCreate
    assert DocumentCreate(name="a.pdf", tags=["acme-ab", "acme-ab", "naas"]).tags == ["acme-ab", "naas"]
