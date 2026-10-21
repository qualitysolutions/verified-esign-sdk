"""Envelopes, documents, files, recipients and the user-info record.

Shapes follow the LIVE API (recorded 2026-10-21), not only the docs. Every read model
keeps unknown fields, and anything not observed yet is optional.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator

from .common import ReadModel, WriteModel
from .tags import validate_tag

#: Signing methods offered by the default envelope descriptor (live, 2026-10-21).
SigningMethod = Literal[
    "bankid-se", "bankid-no", "touch-sign", "email", "sms",
    "nets-eident-fi", "nets-eident-dk", "nets-eident-dk-mitid",
    "eideasy-ee-id", "eideasy-lv-id", "eideasy-lt-id", "eideasy-be-id",
]


# -- users ----------------------------------------------------------------------------
class Role(ReadModel):
    name: str
    label: str | None = None

    @property
    def company_id(self) -> str | None:
        """The company id for a ``/roles/<role>/companies/<id>`` role, else ``None``."""
        if "/companies/" not in self.name:
            return None
        return self.name.rsplit("/companies/", 1)[1].split("/")[0] or None


class UserInfo(ReadModel):
    """``GET /auth/userinfo``."""

    id: str
    uid: str | None = None
    email: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    enabled: bool | None = None
    roles: list[Role] = Field(default_factory=list)

    @property
    def company_ids(self) -> list[str]:
        return sorted({cid for r in self.roles if (cid := r.company_id)})


class Person(ReadModel):
    """The envelope ``sender`` or a file ``owner``."""

    uid: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    email: str | None = None
    namespace: str | None = None


class Sender(WriteModel):
    """Who an envelope is sent from, when not the API user (``POST .../envelopes`` ``sender``).

    In Verified's OpenAPI spec but not in its guides: whether Verified honours it, and
    whether the person must be a user of the company, is checked by the live integration
    test (``test_sender_override``), not assumed.
    """

    email: str
    given_name: str
    family_name: str


# -- recipients -----------------------------------------------------------------------
class RecipientRole(WriteModel):
    """What a recipient does. The default descriptor has ``signer``/``sign`` and
    ``reviewer``/``review`` (``action`` is one of sign, review, edit)."""

    name: str = "signer"
    action: str = "sign"
    label: str | None = None


class RecipientCreate(WriteModel):
    """Body for ``POST /envelopes/{id}/recipients``.

    ``order`` matters with ``sequential_signing``: lower orders are invited first. The
    default descriptor places reviewers at 100 and signers at 100000 when unset.
    ``language`` is required by the API (pattern ``xx_XX``, e.g. ``sv_SE``, ``nb_NO``,
    ``en_EN``). ``telephone`` (with country prefix) is required for SMS signing or SMS
    notification; do not combine SMS notification with SMS signing.
    """

    given_name: str
    family_name: str
    email: str
    signing_method: SigningMethod | str = "bankid-se"
    language: str = Field(default="sv_SE", pattern=r"^[a-z]{2}_[A-Z]{2}$")
    order: int | None = None
    role: RecipientRole = Field(default_factory=RecipientRole)
    telephone: str | None = None
    notification_method: Literal["email", "sms"] | None = None
    #: Personal identity number the eID signature must match (e.g. Swedish personnummer).
    ssn: str | None = None
    secure: bool | None = None


class RecipientUpdate(WriteModel):
    """Body for ``PUT /envelopes/{id}/recipients/{recipient_id}`` (202): only set fields are
    sent. The API does not accept ``telephone``/``notificationMethod`` here. On a PUBLISHED
    envelope, follow up with ``recipients.remind`` so the corrected recipient is notified."""

    partial: ClassVar[bool] = True

    given_name: str | None = None
    family_name: str | None = None
    email: str | None = None
    signing_method: SigningMethod | str | None = None
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}_[A-Z]{2}$")
    order: int | None = None
    role: RecipientRole | None = None
    ssn: str | None = None
    secure: bool | None = None


class Recipient(ReadModel):
    id: str
    uid: str | None = None
    namespace: str | None = None
    version: int | None = None
    given_name: str | None = None
    family_name: str | None = None
    email: str | None = None
    telephone: str | None = None
    role: dict[str, Any] | str = Field(default_factory=dict)
    signing_method: str | None = None
    notification_method: str | None = None
    language: str | None = None
    #: Signing order. Live data holds fractions (e.g. ``100.5``, a recipient inserted between
    #: two others in the web app), so it is a float when read.
    order: float | None = None
    notified: bool = False
    completed: bool = False
    #: Delivery log. ``/query-new`` returns an object keyed by notification id; it is
    #: normalised to a list here.
    notifications: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("notifications", mode="before")
    @classmethod
    def _notifications_as_list(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [v for v in value.values() if isinstance(v, dict)]
        return value if value is not None else []

    @property
    def role_name(self) -> str | None:
        if isinstance(self.role, str):
            return self.role
        name = self.role.get("name")
        return name if isinstance(name, str) else None


# -- documents & files ----------------------------------------------------------------
class DocumentCreate(WriteModel):
    """Body for ``POST /envelopes/{id}/documents``. ``descriptor.hash`` 1 and
    ``source="file"`` are what the default descriptor expects for an uploaded PDF."""

    name: str
    source: str = "file"
    descriptor: dict[str, Any] = Field(default_factory=lambda: {"hash": 1})
    #: Archive/search tags: lowercase, no spaces, at least 3 characters. Build them with
    #: :func:`make_tag` (e.g. the customer company: ``make_tag("Bygg Dialog AB")``).
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(validate_tag(t) for t in value))


class FileRef(ReadModel):
    """A stored file (``GET .../documents/{id}/files`` item, or a document's ``signedFile``)."""

    uid: str | None = None
    id: str | None = None
    name: str | None = None
    file_type: str | None = None
    storage_path: str | None = None
    created: datetime | None = None
    uploaded_at: datetime | None = None

    @property
    def file_id(self) -> str | None:
        if self.id:
            return self.id
        return self.uid.rstrip("/").rsplit("/", 1)[-1] if self.uid else None


class Document(ReadModel):
    id: str
    uid: str | None = None
    namespace: str | None = None
    version: int | None = None
    name: str | None = None
    source: str | None = None
    #: An object from ``GET /envelopes/{id}``; may be a bare id string from ``/query-new``.
    descriptor: dict[str, Any] | str | None = None
    signed: bool = False
    completed: bool = False
    rejected: bool = False
    aborted: bool = False
    trashed: bool = False
    tags: list[str] = Field(default_factory=list)
    #: Present once every recipient has signed: the signed PDF.
    signed_file: FileRef | None = None
    created: datetime | None = None

    @field_validator("signed_file", mode="before")
    @classmethod
    def _file_ref_from_uid(cls, value: Any) -> Any:
        return {"uid": value} if isinstance(value, str) else value


# -- envelopes ------------------------------------------------------------------------
class EnvelopeSettings(WriteModel):
    """Body for ``PUT /envelopes/{id}``: only set fields are sent.

    ``automatic_reminders`` is the number of days before ``expiration`` a reminder goes
    out (0 = none). Never pass a boolean; Verified documents that as undefined behaviour.
    """

    partial: ClassVar[bool] = True

    greeting: str | None = None
    expiration: datetime | None = None
    automatic_reminders: int | None = Field(default=None, ge=0)
    sequential_signing: bool | None = None


class Envelope(ReadModel):
    id: str
    uid: str | None = None
    version: int | None = None
    # ``GET /envelopes/{id}`` embeds objects here; ``GET /query-new/envelopes`` returns
    # bare ids (live 2026-10-21: ``"descriptor": "default"``), so both shapes are accepted.
    flow: dict[str, Any] | str | None = None
    descriptor: dict[str, Any] | str | None = None
    sender: Person | str | None = None
    documents: list[Document] = Field(default_factory=list)
    recipients: list[Recipient] = Field(default_factory=list)
    greeting: str | None = None
    expiration: datetime | None = None
    automatic_reminders: int | None = None
    sequential_signing: bool | None = None
    published: bool = False
    completed: bool = False
    aborted: bool = False
    trashed: bool = False
    expired: bool = False
    last_error: Any = None
    created: datetime | None = None
    modified: datetime | None = None

    @property
    def status(self) -> Literal["draft", "pending", "completed", "aborted", "expired", "trashed"]:
        """One summary state, most final first."""
        if self.completed:
            return "completed"
        if self.aborted:
            return "aborted"
        if self.expired:
            return "expired"
        if self.trashed:
            return "trashed"
        return "pending" if self.published else "draft"


class Created(ReadModel):
    """Result of a create call: the new id (from ``Location``/``uid``) plus the raw uid."""

    id: str
    uid: str | None = None
    location: str | None = None
