"""The one-call send flow: envelope -> settings -> documents + attachments -> recipients -> publish."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from verified_esign import (
    AsyncVerifiedClient,
    EnvelopeSettings,
    RecipientCreate,
    ServerError,
    SigningDocument,
    VerifiedClient,
)

from .conftest import BASE, PDF

ENV = "E1"
SIGNER = RecipientCreate(given_name="Anna", family_name="Andersson", email="anna@kund.se")


def _mock_flow(respx_mock: Any) -> dict[str, Any]:
    routes = {
        "create": respx_mock.post(f"{BASE}/envelope-descriptors/default/envelopes").mock(
            return_value=httpx.Response(201, json={"uid": f"/envelopes/{ENV}"},
                                        headers={"location": f"/api/envelopes/{ENV}"})),
        "settings": respx_mock.put(f"{BASE}/envelopes/{ENV}").mock(return_value=httpx.Response(202)),
        "doc": respx_mock.post(f"{BASE}/envelopes/{ENV}/documents").mock(side_effect=[
            httpx.Response(200, headers={"location": f"/api/envelopes/{ENV}/documents/D{i}"}) for i in (1, 2)]),
        "recipient": respx_mock.post(f"{BASE}/envelopes/{ENV}/recipients").mock(
            return_value=httpx.Response(200, headers={"location": f"/api/envelopes/{ENV}/recipients/R1"})),
        "publish": respx_mock.put(f"{BASE}/envelopes/{ENV}/publish-status").mock(return_value=httpx.Response(202)),
        "delete": respx_mock.delete(f"{BASE}/envelopes/{ENV}").mock(return_value=httpx.Response(200)),
    }
    counter = {"n": 0}

    def upload(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"id": f"F{counter['n']}"})

    routes["upload"] = respx_mock.post(url__regex=rf"{BASE}/envelopes/{ENV}/documents/D\d/files/\w+").mock(
        side_effect=upload)
    return routes


def test_send_with_attachments(client: VerifiedClient, respx_mock: Any) -> None:
    routes = _mock_flow(respx_mock)
    sent = client.send_for_signing(
        SigningDocument("Avtal.pdf", PDF, attachments=[("Bilaga 1.pdf", PDF), ("Bilaga 2.xlsx", b"PK..")]),
        [SIGNER],
        settings=EnvelopeSettings(greeting="Hej"),
        tags=["naas-avtal"],
    )
    assert sent.envelope_id == ENV and sent.document_ids == ["D1"] and sent.recipient_ids == ["R1"]
    assert sent.published and sent.file_ids == {"D1": "F1"} and sent.attachment_ids == {"D1": ["F2", "F3"]}
    paths = [c.request.url.path for c in routes["upload"].calls]
    assert paths == [f"/api/envelopes/{ENV}/documents/D1/files/document",
                     f"/api/envelopes/{ENV}/documents/D1/files/attachment",
                     f"/api/envelopes/{ENV}/documents/D1/files/attachment"]
    assert json.loads(routes["doc"].calls.last.request.content)["tags"] == ["naas-avtal"]
    assert routes["publish"].called and not routes["delete"].called


def test_tuple_and_list_forms(client: VerifiedClient, respx_mock: Any) -> None:
    _mock_flow(respx_mock)
    sent = client.send_for_signing([("A.pdf", PDF), SigningDocument("B.pdf", PDF)], [SIGNER], publish=False)
    assert sent.document_ids == ["D1", "D2"] and sent.published is False


def test_draft_is_not_published(client: VerifiedClient, respx_mock: Any) -> None:
    routes = _mock_flow(respx_mock)
    client.send_for_signing(("Avtal.pdf", PDF), [SIGNER], publish=False)
    assert not routes["publish"].called


def test_failure_deletes_half_built_draft(client: VerifiedClient, respx_mock: Any) -> None:
    routes = _mock_flow(respx_mock)
    routes["recipient"].mock(return_value=httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(ServerError):
        client.send_for_signing(("Avtal.pdf", PDF), [SIGNER])
    assert routes["delete"].called and not routes["publish"].called


def test_validation_happens_before_anything_is_created(client: VerifiedClient, respx_mock: Any) -> None:
    routes = _mock_flow(respx_mock)
    with pytest.raises(ValueError, match="20 per document"):
        client.send_for_signing(SigningDocument("A.pdf", PDF, attachments=[("x.pdf", PDF)] * 21), [SIGNER])
    with pytest.raises(ValueError, match="recipient"):
        client.send_for_signing(("A.pdf", PDF), [])
    assert not routes["create"].called


async def test_async_send(async_client: AsyncVerifiedClient, respx_mock: Any) -> None:
    routes = _mock_flow(respx_mock)
    sent = await async_client.send_for_signing(
        SigningDocument("Avtal.pdf", PDF, attachments=[("Bilaga.pdf", PDF)]), [SIGNER])
    assert sent.attachment_ids == {"D1": ["F2"]} and routes["publish"].called


def test_company_tag_on_every_document(client: VerifiedClient, respx_mock: Any) -> None:
    from verified_esign import make_tag
    routes = _mock_flow(respx_mock)
    client.send_for_signing([("A.pdf", PDF), ("B.pdf", PDF)], [SIGNER], tags=[make_tag("Bygg Dialog AB")])
    assert [json.loads(c.request.content)["tags"] for c in routes["doc"].calls] == [
        ["bygg-dialog-ab"], ["bygg-dialog-ab"]]


def test_bad_tag_rejected_before_anything_is_created(client: VerifiedClient, respx_mock: Any) -> None:
    routes = _mock_flow(respx_mock)
    with pytest.raises(ValueError, match="Invalid Verified tag"):
        client.send_for_signing(("A.pdf", PDF), [SIGNER], tags=["Bygg Dialog AB"])
    assert not routes["create"].called


def test_sender_reaches_the_envelope(client: VerifiedClient, respx_mock: Any) -> None:
    from verified_esign import Sender
    routes = _mock_flow(respx_mock)
    client.send_for_signing(("A.pdf", PDF), [SIGNER], publish=False,
                            sender=Sender(email="saljare@qls.se", given_name="Sven", family_name="Säljare"))
    assert json.loads(routes["create"].calls.last.request.content)["sender"]["email"] == "saljare@qls.se"
