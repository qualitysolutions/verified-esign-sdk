"""Public sync and async clients for the Verified eSign API."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx

from ._transport import AsyncTransport, Response, SyncTransport
from .config import Config
from .models.envelopes import DocumentCreate, EnvelopeSettings, RecipientCreate, Sender, UserInfo
from .models.tags import validate_tag
from .resources import (
    AsyncDocuments,
    AsyncEnvelopes,
    AsyncRecipients,
    Documents,
    Envelopes,
    Recipients,
)


def _coerce_config(config: Config | None, kwargs: dict[str, Any]) -> Config:
    if config is not None:
        if kwargs:
            raise TypeError("Pass either a Config or credential keyword arguments, not both")
        return config
    return Config.from_env(**kwargs)


@dataclass(frozen=True)
class SentEnvelope:
    """What :meth:`VerifiedClient.send_for_signing` built."""

    envelope_id: str
    document_ids: list[str]
    recipient_ids: list[str]
    published: bool
    #: Document id -> uploaded file id.
    file_ids: dict[str, str] = field(default_factory=dict)
    #: Document id -> uploaded attachment file ids, in the order given.
    attachment_ids: dict[str, list[str]] = field(default_factory=dict)


FileContent = bytes | str | Path


@dataclass(frozen=True)
class SigningDocument:
    """A PDF to sign plus its attachments (bilagor).

    Attachments travel with the document and are shown to the signers, but are NOT
    themselves signed: only ``content`` is. Anything that must be legally covered by the
    signature belongs inside the PDF. Up to 20 attachments per document; allowed types:
    pdf, docx, xlsx, pptx, zip, png, gif, jpg, rtf, odt.
    """

    name: str
    content: FileContent
    attachments: Sequence[tuple[str, FileContent]] = ()


DocumentsArg = SigningDocument | tuple[str, FileContent] | Sequence[SigningDocument | tuple[str, FileContent]]

#: Verified's per-document attachment cap (default descriptor).
MAX_ATTACHMENTS_PER_DOCUMENT = 20


def _documents_arg(documents: DocumentsArg) -> list[SigningDocument]:
    if isinstance(documents, SigningDocument):
        items: list[SigningDocument | tuple[str, FileContent]] = [documents]
    elif isinstance(documents, tuple) and len(documents) == 2 and isinstance(documents[0], str):
        items = [documents]
    else:
        items = list(documents)
    docs = [d if isinstance(d, SigningDocument) else SigningDocument(name=d[0], content=d[1]) for d in items]
    if not docs:
        raise ValueError("At least one document is required")
    for d in docs:
        if len(d.attachments) > MAX_ATTACHMENTS_PER_DOCUMENT:
            raise ValueError(f"{d.name}: {len(d.attachments)} attachments; Verified allows "
                             f"{MAX_ATTACHMENTS_PER_DOCUMENT} per document")
    return docs


class VerifiedClient:
    """Synchronous client for the Verified eSign API.

    Construct with a :class:`Config`, with keyword arguments, or with nothing (reads
    ``VERIFIED_*`` environment variables)::

        with VerifiedClient(email="api@qls.se", password="...", company_id="HyTjgHzt4") as v:
            sent = v.send_for_signing(
                ("Avtal AVT-2026-0042.pdf", pdf_bytes),
                [RecipientCreate(given_name="Anna", family_name="Andersson", email="anna@kund.se")],
                settings=EnvelopeSettings(greeting="Hej! Här är avtalet för signering."),
            )
    """

    def __init__(self, config: Config | None = None, *, http_client: httpx.Client | None = None,
                 **credentials: Any) -> None:
        self._config = _coerce_config(config, credentials)
        self._transport = SyncTransport(self._config, client=http_client)
        self.envelopes = Envelopes(self._transport)
        self.documents = Documents(self._transport)
        self.recipients = Recipients(self._transport)

    @property
    def config(self) -> Config:
        return self._config

    @property
    def namespace(self) -> str:
        """The ``x-namespace`` in use (resolved from userinfo if no company_id was set)."""
        return self._transport.get_namespace()

    def userinfo(self) -> UserInfo:
        return UserInfo.model_validate(self._transport.request("GET", "/auth/userinfo", namespace=False).body)

    def renew_token(self) -> None:
        """Renew the cached JWT via ``GET /auth/token`` (normally not needed: the SDK logs in
        again on its own before the token expires)."""
        self._transport.renew_token()

    def send_for_signing(
        self,
        documents: DocumentsArg,
        recipients: Sequence[RecipientCreate],
        *,
        settings: EnvelopeSettings | None = None,
        tags: Sequence[str] | None = None,
        publish: bool = True,
        sender: Sender | None = None,
    ) -> SentEnvelope:
        """Create an envelope, upload the PDF(s), add recipients and publish it.

        ``documents`` is a :class:`SigningDocument` (PDF + attachments), a ``(name, pdf)``
        tuple, or a list of either; file content is bytes or a path. ``tags`` are set on
        every document and validated locally; build them with :func:`make_tag`, e.g.
        ``tags=[make_tag(customer_name)]`` so the company's contracts are searchable.
        ``sender`` names who the envelope is sent from (default: the API user).
        With ``publish=False`` the envelope stays a draft and nobody is notified. If a step
        fails before publishing, the half-built draft is deleted and the error re-raised.
        """
        docs = _documents_arg(documents)
        if not recipients:
            raise ValueError("At least one recipient is required")
        tag_list = [validate_tag(t) for t in tags] if tags else None
        envelope_id = self.envelopes.create(sender=sender).id
        try:
            if settings is not None:
                self.envelopes.update(envelope_id, settings)
            document_ids: list[str] = []
            file_ids: dict[str, str] = {}
            attachment_ids: dict[str, list[str]] = {}
            for doc in docs:
                doc_id = self.documents.create(
                    envelope_id, DocumentCreate(name=doc.name, tags=tag_list)).id
                file_ids[doc_id] = self.documents.upload(envelope_id, doc_id, doc.content, name=doc.name)
                attachment_ids[doc_id] = [
                    self.documents.upload(envelope_id, doc_id, content, name=name, file_type="attachment")
                    for name, content in doc.attachments
                ]
                document_ids.append(doc_id)
            recipient_ids = [self.recipients.add(envelope_id, r).id for r in recipients]
        except BaseException:
            try:
                self.envelopes.delete(envelope_id)
            except Exception:  # noqa: BLE001 - the original error matters more
                pass
            raise
        if publish:
            self.envelopes.publish(envelope_id)
        return SentEnvelope(envelope_id=envelope_id, document_ids=document_ids,
                            recipient_ids=recipient_ids, published=publish, file_ids=file_ids,
                            attachment_ids=attachment_ids)

    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None,
                headers: Mapping[str, str] | None = None, namespace: bool = True) -> Response:
        """Escape hatch: an authenticated call to any path (relative to ``base_url``)."""
        return self._transport.request(method, path, json=json, params=params, headers=headers,
                                       namespace=namespace)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "VerifiedClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncVerifiedClient:
    """Asynchronous client; mirrors :class:`VerifiedClient` with coroutine methods."""

    def __init__(self, config: Config | None = None, *, http_client: httpx.AsyncClient | None = None,
                 **credentials: Any) -> None:
        self._config = _coerce_config(config, credentials)
        self._transport = AsyncTransport(self._config, client=http_client)
        self.envelopes = AsyncEnvelopes(self._transport)
        self.documents = AsyncDocuments(self._transport)
        self.recipients = AsyncRecipients(self._transport)

    @property
    def config(self) -> Config:
        return self._config

    async def namespace(self) -> str:
        return await self._transport.get_namespace()

    async def userinfo(self) -> UserInfo:
        response = await self._transport.request("GET", "/auth/userinfo", namespace=False)
        return UserInfo.model_validate(response.body)

    async def renew_token(self) -> None:
        await self._transport.renew_token()

    async def send_for_signing(
        self,
        documents: DocumentsArg,
        recipients: Sequence[RecipientCreate],
        *,
        settings: EnvelopeSettings | None = None,
        tags: Sequence[str] | None = None,
        publish: bool = True,
        sender: Sender | None = None,
    ) -> SentEnvelope:
        """See :meth:`VerifiedClient.send_for_signing`."""
        docs = _documents_arg(documents)
        if not recipients:
            raise ValueError("At least one recipient is required")
        tag_list = [validate_tag(t) for t in tags] if tags else None
        envelope_id = (await self.envelopes.create(sender=sender)).id
        try:
            if settings is not None:
                await self.envelopes.update(envelope_id, settings)
            document_ids: list[str] = []
            file_ids: dict[str, str] = {}
            attachment_ids: dict[str, list[str]] = {}
            for doc in docs:
                doc_id = (await self.documents.create(
                    envelope_id, DocumentCreate(name=doc.name, tags=tag_list))).id
                file_ids[doc_id] = await self.documents.upload(envelope_id, doc_id, doc.content, name=doc.name)
                attachment_ids[doc_id] = [
                    await self.documents.upload(envelope_id, doc_id, content, name=name, file_type="attachment")
                    for name, content in doc.attachments
                ]
                document_ids.append(doc_id)
            recipient_ids = [(await self.recipients.add(envelope_id, r)).id for r in recipients]
        except BaseException:
            try:
                await self.envelopes.delete(envelope_id)
            except Exception:  # noqa: BLE001 - the original error matters more
                pass
            raise
        if publish:
            await self.envelopes.publish(envelope_id)
        return SentEnvelope(envelope_id=envelope_id, document_ids=document_ids,
                            recipient_ids=recipient_ids, published=publish, file_ids=file_ids,
                            attachment_ids=attachment_ids)

    async def request(self, method: str, path: str, *, json: Any = None,
                      params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
                      namespace: bool = True) -> Response:
        return await self._transport.request(method, path, json=json, params=params, headers=headers,
                                             namespace=namespace)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> "AsyncVerifiedClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
