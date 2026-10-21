"""Document tags.

Verified indexes document tags for archive search and the query API, and requires them
to be lowercase, without spaces and at least 3 characters long. :func:`make_tag` turns a
display value such as a company name into such a tag; :func:`validate_tag` rejects
anything else before it reaches the API.
"""

from __future__ import annotations

import re
import unicodedata

#: Lowercase letters/digits, with ``-`` or ``_`` between them; at least 3 characters.
TAG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")
MIN_TAG_LENGTH = 3


def make_tag(value: str) -> str:
    """Normalise a display value into a Verified tag.

    ``"Bygg Dialog AB"`` -> ``"bygg-dialog-ab"``, ``"Ölands Åkeri & Bär AB"`` ->
    ``"olands-akeri-bar-ab"``. Diacritics are folded to ASCII (å/ä -> a, ö -> o, é -> e),
    every run of other characters becomes one ``-``. Deterministic, so the same company
    always gets the same tag. Raises ``ValueError`` if fewer than 3 characters remain.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    tag = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return validate_tag(tag)


def validate_tag(tag: str) -> str:
    """Return ``tag`` unchanged if Verified accepts it, else raise ``ValueError``."""
    if len(tag) < MIN_TAG_LENGTH or not TAG_PATTERN.match(tag):
        raise ValueError(
            f"Invalid Verified tag {tag!r}: use lowercase letters/digits with - or _, "
            f"at least {MIN_TAG_LENGTH} characters (see make_tag())"
        )
    return tag
