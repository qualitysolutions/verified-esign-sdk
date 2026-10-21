"""``client.envelopes``, ``client.documents`` and ``client.recipients``.

Writes to Verified are CQRS commands: settings updates answer ``202 Accepted`` with an
empty body and are applied shortly after, so these methods return ``None`` and callers
that need the new state call ``get``. Created documents and recipients return their id
only in the ``Location`` header; the SDK hands back a :class:`Created`.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

from .._transport import AsyncTransport, Response, SyncTransport
from ..models.envelopes import (
    Created,
    Document,
    DocumentCreate,
    Envelope,
    EnvelopeSettings,
    FileRef,
    Recipient,
    RecipientCreate,
    RecipientUpdate,
)
from ..errors import VerifiedError
from ._base import (
    as_list,
    created,
    document_path,
    envelope_path,
    file_id_from_upload,
    file_part,
    presigned_url,
    recipient_path,
    seg,
)


#: Largest page ``GET /query-new/envelopes`` returns.
MAX_QUERY_LIMIT = 100


def _comment(comment: str | None) -> dict[str, Any]:
    return {"comment": comment} if comment else {}


def _query_params(filters: Mapping[str, Any] | None, offset: int, limit: int,
                  sort: Mapping[str, int] | None) -> dict[str, str]:
    if not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    params = {"from": str(offset), "limit": str(limit)}
    if filters:
        params["filters"] = json.dumps(_logical_root(filters), separators=(",", ":"), default=str)
    if sort:
        params["sort"] = json.dumps(dict(sort), separators=(",", ":"))
    return params


_LOGICAL_KEYS = frozenset({"and", "or", "nor"})


def _logical_root(filters: Mapping[str, Any]) -> dict[str, Any]:
    """Give the filter an ``and``/``or``/``nor`` root, the only form Verified's docs show.
    Live 2026-10-21 a bare ``{"field", "op", "value"}`` tag filter returned untagged
    envelopes, i.e. it looked ignored; wrapping it in ``{"and": [...]}`` is the fix
    being verified by the integration test."""
    if set(filters) & _LOGICAL_KEYS:
        return dict(filters)
    if "field" in filters:
        return {"and": [dict(filters)]}
    raise ValueError("filters must be a {'field','op','value'} statement or an and/or/nor group")


def _sign_link_body(recipient_id: str, redirect_to: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"recipient": {"id": recipient_id}}
    if redirect_to:
        if not redirect_to.startswith("https://"):
            raise ValueError("redirect_to must be an absolute https:// URL")
        body["redirectTo"] = redirect_to
    return body


def _sign_link_url(response: Response, recipient_id: str) -> str:
    body = response.body if isinstance(response.body, dict) else {}
    links = (body.get("getSignLink") or {}).get("recipient") or {}
    entry = links.get(recipient_id) or (next(iter(links.values())) if len(links) == 1 else None)
    url = entry.get("url") if isinstance(entry, dict) else None
    if not isinstance(url, str) or not url:
        raise VerifiedError(f"No signing link returned for recipient {recipient_id}; is the envelope "
                            "published and its document uploaded?")
    return url


def _remind_body(recipient_id: str, greeting: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"recipient": {"id": recipient_id}}
    if greeting:
        body["envelope"] = {"greeting": greeting}
    return body


def _signed_file_id(document: Document) -> str:
    if document.signed_file is None or not document.signed_file.file_id:
        raise VerifiedError(
            f"Document {document.id} has no signed file yet (signed={document.signed}); "
            "wait for the envelope to complete"
        )
    return document.signed_file.file_id


# -- sync ------------------------------------------------------------------------------
class Envelopes:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def create(self, *, descriptor: str = "default") -> Created:
        """Create an empty draft envelope from an envelope descriptor."""
        return created(self._t.request("POST", f"/envelope-descriptors/{seg(descriptor)}/envelopes", json={}))

    def get(self, envelope_id: str) -> Envelope:
        return Envelope.model_validate(self._t.request("GET", envelope_path(envelope_id)).body)

    def update(self, envelope_id: str, settings: EnvelopeSettings) -> None:
        """Change greeting / expiration / reminders / sequential signing (applied async)."""
        self._t.request("PUT", envelope_path(envelope_id), json=settings.to_body())

    def publish(self, envelope_id: str) -> None:
        """Start the signing process. **Invitations are sent to the recipients.**"""
        self._t.request("PUT", f"{envelope_path(envelope_id)}/publish-status", json={"published": True})

    def abort(self, envelope_id: str, *, comment: str | None = None) -> None:
        """Withdraw a published envelope (not possible once completed, rejected or trashed)."""
        self._t.request("PUT", f"{envelope_path(envelope_id)}/abort-status", json=_comment(comment))

    def trash(self, envelope_id: str, *, comment: str | None = None) -> None:
        """Hide the envelope from the dashboard (moves it to the archive)."""
        self._t.request("PUT", f"{envelope_path(envelope_id)}/trash-status", json=_comment(comment))

    def query(self, filters: Mapping[str, Any] | None = None, *, offset: int = 0, limit: int = MAX_QUERY_LIMIT,
              sort: Mapping[str, int] | None = None) -> list[Envelope]:
        """Search envelopes (``GET /query-new/envelopes``). ``filters`` uses Verified's
        Mongo-like syntax, e.g. ``{"and": [{"field": "completed", "op": "eq", "value": True}]}``;
        a single bare statement is wrapped in ``and`` (Verified ignores it otherwise).
        ``sort`` e.g. ``{"modified": -1}``. Only this endpoint computes ``expired`` live.
        The search index is updated asynchronously, so a just-created envelope may take a
        moment to appear."""
        response = self._t.request("GET", "/query-new/envelopes",
                                   params=_query_params(filters, offset, limit, sort))
        return [Envelope.model_validate(e) for e in as_list(response)]

    def iter(self, filters: Mapping[str, Any] | None = None, *, page_size: int = MAX_QUERY_LIMIT,
             sort: Mapping[str, int] | None = None) -> Iterator[Envelope]:
        """Iterate over every matching envelope, page by page."""
        offset = 0
        while True:
            page = self.query(filters, offset=offset, limit=page_size, sort=sort)
            yield from page
            if len(page) < page_size:
                return
            offset += len(page)

    def delete(self, envelope_id: str) -> None:
        self._t.request("DELETE", envelope_path(envelope_id))


class Documents:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def create(self, envelope_id: str, document: DocumentCreate) -> Created:
        return created(self._t.request("POST", f"{envelope_path(envelope_id)}/documents", json=document.to_body()))

    def list(self, envelope_id: str) -> list[Document]:
        response = self._t.request("GET", f"{envelope_path(envelope_id)}/documents")
        return [Document.model_validate(d) for d in as_list(response)]

    def get(self, envelope_id: str, document_id: str) -> Document:
        return Document.model_validate(self._t.request("GET", document_path(envelope_id, document_id)).body)

    def upload(self, envelope_id: str, document_id: str, content: bytes | str | Path, *,
               name: str | None = None, file_type: str = "document") -> str:
        """Upload the PDF to sign (``file_type="document"``) or an attachment; returns the file id.

        Validated locally first: documents must be PDF and at most 10 MB."""
        files, data = file_part(content, name, file_type)
        response = self._t.request("POST", f"{document_path(envelope_id, document_id)}/files/{file_type}",
                                   files=files, data=data)
        return file_id_from_upload(response)

    def list_files(self, envelope_id: str, document_id: str) -> builtins.list[FileRef]:
        response = self._t.request("GET", f"{document_path(envelope_id, document_id)}/files")
        return [FileRef.model_validate(f) for f in as_list(response)]

    def file_url(self, envelope_id: str, document_id: str, file_id: str) -> str:
        """A temporary, unauthenticated download URL for a file."""
        response = self._t.request("GET", f"{document_path(envelope_id, document_id)}/files/{seg(file_id)}/url",
                                   params={"asObject": "true"})
        return presigned_url(response)

    def download(self, envelope_id: str, document_id: str, file_id: str) -> bytes:
        return self._t.fetch_presigned(self.file_url(envelope_id, document_id, file_id))

    def download_signed(self, envelope_id: str, document_id: str) -> bytes:
        """The signed PDF. Raises :class:`VerifiedError` until every recipient has signed."""
        return self.download(envelope_id, document_id, _signed_file_id(self.get(envelope_id, document_id)))


class Recipients:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def add(self, envelope_id: str, recipient: RecipientCreate) -> Created:
        return created(self._t.request("POST", f"{envelope_path(envelope_id)}/recipients",
                                       json=recipient.to_body()))

    def list(self, envelope_id: str) -> list[Recipient]:
        response = self._t.request("GET", f"{envelope_path(envelope_id)}/recipients")
        return [Recipient.model_validate(r) for r in as_list(response)]

    def get(self, envelope_id: str, recipient_id: str) -> Recipient:
        return Recipient.model_validate(self._t.request("GET", recipient_path(envelope_id, recipient_id)).body)

    def update(self, envelope_id: str, recipient_id: str, changes: RecipientUpdate) -> None:
        self._t.request("PUT", recipient_path(envelope_id, recipient_id), json=changes.to_body())

    def sign_link(self, envelope_id: str, recipient_id: str, *, redirect_to: str | None = None) -> str:
        """A personal signing URL for an embedded / redirect flow. Only valid once the
        envelope is published and its document uploaded. **The URL carries an access token:
        treat it as a secret** (do not log it)."""
        response = self._t.request("POST", f"{envelope_path(envelope_id)}/jobs/get.sign.link",
                                   json=_sign_link_body(recipient_id, redirect_to))
        return _sign_link_url(response, recipient_id)

    def remind(self, envelope_id: str, recipient_id: str, *, greeting: str | None = None) -> None:
        """Re-send the invitation (also required after correcting a published recipient)."""
        self._t.request("POST", f"{envelope_path(envelope_id)}/jobs/send.notification",
                        json=_remind_body(recipient_id, greeting))


# -- async -----------------------------------------------------------------------------
class AsyncEnvelopes:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(self, *, descriptor: str = "default") -> Created:
        return created(await self._t.request("POST", f"/envelope-descriptors/{seg(descriptor)}/envelopes", json={}))

    async def get(self, envelope_id: str) -> Envelope:
        return Envelope.model_validate((await self._t.request("GET", envelope_path(envelope_id))).body)

    async def update(self, envelope_id: str, settings: EnvelopeSettings) -> None:
        await self._t.request("PUT", envelope_path(envelope_id), json=settings.to_body())

    async def publish(self, envelope_id: str) -> None:
        await self._t.request("PUT", f"{envelope_path(envelope_id)}/publish-status", json={"published": True})

    async def abort(self, envelope_id: str, *, comment: str | None = None) -> None:
        await self._t.request("PUT", f"{envelope_path(envelope_id)}/abort-status", json=_comment(comment))

    async def trash(self, envelope_id: str, *, comment: str | None = None) -> None:
        await self._t.request("PUT", f"{envelope_path(envelope_id)}/trash-status", json=_comment(comment))

    async def query(self, filters: Mapping[str, Any] | None = None, *, offset: int = 0,
                    limit: int = MAX_QUERY_LIMIT, sort: Mapping[str, int] | None = None) -> list[Envelope]:
        response = await self._t.request("GET", "/query-new/envelopes",
                                         params=_query_params(filters, offset, limit, sort))
        return [Envelope.model_validate(e) for e in as_list(response)]

    async def iter(self, filters: Mapping[str, Any] | None = None, *, page_size: int = MAX_QUERY_LIMIT,
                   sort: Mapping[str, int] | None = None) -> AsyncIterator[Envelope]:
        offset = 0
        while True:
            page = await self.query(filters, offset=offset, limit=page_size, sort=sort)
            for envelope in page:
                yield envelope
            if len(page) < page_size:
                return
            offset += len(page)

    async def delete(self, envelope_id: str) -> None:
        await self._t.request("DELETE", envelope_path(envelope_id))


class AsyncDocuments:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(self, envelope_id: str, document: DocumentCreate) -> Created:
        return created(await self._t.request("POST", f"{envelope_path(envelope_id)}/documents",
                                             json=document.to_body()))

    async def list(self, envelope_id: str) -> list[Document]:
        response = await self._t.request("GET", f"{envelope_path(envelope_id)}/documents")
        return [Document.model_validate(d) for d in as_list(response)]

    async def get(self, envelope_id: str, document_id: str) -> Document:
        return Document.model_validate((await self._t.request("GET", document_path(envelope_id, document_id))).body)

    async def upload(self, envelope_id: str, document_id: str, content: bytes | str | Path, *,
                     name: str | None = None, file_type: str = "document") -> str:
        files, data = file_part(content, name, file_type)
        response = await self._t.request("POST", f"{document_path(envelope_id, document_id)}/files/{file_type}",
                                         files=files, data=data)
        return file_id_from_upload(response)

    async def list_files(self, envelope_id: str, document_id: str) -> builtins.list[FileRef]:
        response = await self._t.request("GET", f"{document_path(envelope_id, document_id)}/files")
        return [FileRef.model_validate(f) for f in as_list(response)]

    async def file_url(self, envelope_id: str, document_id: str, file_id: str) -> str:
        response = await self._t.request("GET", f"{document_path(envelope_id, document_id)}/files/{seg(file_id)}/url",
                                         params={"asObject": "true"})
        return presigned_url(response)

    async def download(self, envelope_id: str, document_id: str, file_id: str) -> bytes:
        return await self._t.fetch_presigned(await self.file_url(envelope_id, document_id, file_id))

    async def download_signed(self, envelope_id: str, document_id: str) -> bytes:
        document = await self.get(envelope_id, document_id)
        return await self.download(envelope_id, document_id, _signed_file_id(document))


class AsyncRecipients:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def add(self, envelope_id: str, recipient: RecipientCreate) -> Created:
        return created(await self._t.request("POST", f"{envelope_path(envelope_id)}/recipients",
                                             json=recipient.to_body()))

    async def list(self, envelope_id: str) -> list[Recipient]:
        response = await self._t.request("GET", f"{envelope_path(envelope_id)}/recipients")
        return [Recipient.model_validate(r) for r in as_list(response)]

    async def get(self, envelope_id: str, recipient_id: str) -> Recipient:
        return Recipient.model_validate((await self._t.request("GET", recipient_path(envelope_id, recipient_id))).body)

    async def update(self, envelope_id: str, recipient_id: str, changes: RecipientUpdate) -> None:
        await self._t.request("PUT", recipient_path(envelope_id, recipient_id), json=changes.to_body())

    async def sign_link(self, envelope_id: str, recipient_id: str, *, redirect_to: str | None = None) -> str:
        response = await self._t.request("POST", f"{envelope_path(envelope_id)}/jobs/get.sign.link",
                                         json=_sign_link_body(recipient_id, redirect_to))
        return _sign_link_url(response, recipient_id)

    async def remind(self, envelope_id: str, recipient_id: str, *, greeting: str | None = None) -> None:
        await self._t.request("POST", f"{envelope_path(envelope_id)}/jobs/send.notification",
                              json=_remind_body(recipient_id, greeting))
