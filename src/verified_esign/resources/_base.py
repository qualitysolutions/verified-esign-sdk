"""Shared request building and response parsing for the resource namespaces.

The sync and async namespaces only differ in ``await``; everything they share lives here
as free functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from .._transport import FilePart, Response
from ..errors import VerifiedError
from ..models.common import id_from_uid
from ..models.envelopes import Created

#: Largest document Verified accepts (NETS signing methods: 3.5 MB).
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
#: File extensions Verified accepts as attachments.
ATTACHMENT_EXTENSIONS = frozenset({"pdf", "docx", "xlsx", "pptx", "zip", "png", "gif", "jpg", "rtf", "odt"})

_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip", "png": "image/png", "gif": "image/gif", "jpg": "image/jpeg",
    "rtf": "application/rtf", "odt": "application/vnd.oasis.opendocument.text",
}


def seg(value: str) -> str:
    """Quote one path segment (ids are opaque; never let one inject a path)."""
    if not value:
        raise ValueError("id must not be empty")
    return quote(value, safe="")


def envelope_path(envelope_id: str) -> str:
    return f"/envelopes/{seg(envelope_id)}"


def document_path(envelope_id: str, document_id: str) -> str:
    return f"{envelope_path(envelope_id)}/documents/{seg(document_id)}"


def recipient_path(envelope_id: str, recipient_id: str) -> str:
    return f"{envelope_path(envelope_id)}/recipients/{seg(recipient_id)}"


def created(response: Response) -> Created:
    """Read the new id from ``Location`` (documents/recipients have NO body) or ``uid``."""
    body = response.body if isinstance(response.body, dict) else {}
    uid = body.get("uid") if isinstance(body.get("uid"), str) else None
    new_id = id_from_uid(response.location) or id_from_uid(uid)
    if not new_id:
        raise VerifiedError(f"Create returned HTTP {response.status_code} without a Location or uid")
    return Created(id=new_id, uid=uid, location=response.location)


def as_list(response: Response) -> list[Any]:
    if response.body is None:
        return []
    if not isinstance(response.body, list):
        raise VerifiedError(f"Expected a JSON array, got {type(response.body).__name__}")
    return response.body


def file_part(content: bytes | str | Path, name: str | None, file_type: str) -> tuple[dict[str, FilePart], dict[str, str]]:
    """Validate an upload locally (type, size, PDF magic) and build the multipart fields."""
    if isinstance(content, (str, Path)):
        path = Path(content)
        data = path.read_bytes()
        name = name or path.name
    else:
        data = content
    if not name:
        raise ValueError("name is required when uploading raw bytes")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if file_type == "document":
        if ext != "pdf" or not data.startswith(b"%PDF-"):
            raise ValueError("Documents to sign must be PDF files (name *.pdf, content starting with %PDF-)")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"Document is {len(data)} bytes; Verified accepts at most {MAX_DOCUMENT_BYTES}")
    elif file_type == "attachment":
        if ext not in ATTACHMENT_EXTENSIONS:
            raise ValueError(f"Attachment type .{ext} not accepted; allowed: {sorted(ATTACHMENT_EXTENSIONS)}")
    else:
        raise ValueError("file_type must be 'document' or 'attachment'")
    return {"file": (name, data, _MIME.get(ext, "application/octet-stream"))}, {"name": name}


def file_id_from_upload(response: Response) -> str:
    body = response.body
    if isinstance(body, dict) and isinstance(body.get("id"), str):
        return str(body["id"])
    raise VerifiedError(f"Upload returned HTTP {response.status_code} without a file id")


def presigned_url(response: Response) -> str:
    body = response.body
    if isinstance(body, dict) and isinstance(body.get("url"), str):
        return str(body["url"])
    raise VerifiedError("File URL response did not contain a url")
