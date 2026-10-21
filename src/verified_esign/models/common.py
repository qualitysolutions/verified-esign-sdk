"""Shared model plumbing.

Verified's JSON is camelCase; the models expose snake_case attributes and accept either
form. Read models keep unknown fields (``model_extra``) so new API fields never break
parsing; write models forbid unknown fields so typos fail locally, not at the API.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ReadModel(BaseModel):
    """Base for parsed API responses: camelCase aliases, unknown fields kept."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class WriteModel(BaseModel):
    """Base for request bodies: camelCase on the wire.

    Create bodies (``partial = False``) send every non-null field INCLUDING defaults, so
    e.g. a recipient's default ``language`` reaches the API, which requires it. Update
    bodies (``partial = True``) send only the fields the caller set.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    partial: ClassVar[bool] = False

    def to_body(self) -> dict[str, Any]:
        if self.partial:
            return self.model_dump(by_alias=True, exclude_unset=True, mode="json")
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def id_from_uid(uid: str | None) -> str | None:
    """The last path segment of a Verified ``uid``/``Location`` (``/api/envelopes/X`` -> ``X``)."""
    if not uid:
        return None
    return uid.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or None
