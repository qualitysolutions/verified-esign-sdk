"""Exception hierarchy for the Verified eSign SDK.

Every error raised by the SDK derives from :class:`VerifiedError`. Non-2xx API responses
raise an :class:`APIStatusError` subclass carrying the status code and the parsed body.
Verified does not document a structured error format, so the body is kept as-is
(JSON or text) and the message is taken from a ``message``/``error`` field when present.
"""

from __future__ import annotations

from typing import Any


class VerifiedError(Exception):
    """Base class for every error raised by this SDK."""


class ConfigurationError(VerifiedError):
    """Raised when the client is misconfigured (missing credentials, ambiguous company)."""


class AuthenticationError(VerifiedError):
    """Raised when ``POST /auth`` does not return a token (wrong email/password, locked user)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WebhookVerificationError(VerifiedError):
    """Raised when an incoming webhook call fails authentication or cannot be parsed."""


class APIError(VerifiedError):
    """Base class for errors returned by an API call."""


class APIConnectionError(APIError):
    """Raised when the request could not be sent or no response was received."""


class APITimeoutError(APIConnectionError):
    """Raised when a request exceeds the configured timeout."""


class APIStatusError(APIError):
    """Raised when the API returns a non-2xx HTTP status.

    Attributes
    ----------
    status_code:
        The HTTP status code.
    body:
        The raw parsed body (JSON or text).
    method, path:
        The request that failed (path relative to ``base_url``, no query string).
    """

    def __init__(self, message: str, *, status_code: int, body: Any = None,
                 method: str | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.method = method
        self.path = path

    def __str__(self) -> str:
        base = super().__str__()
        where = f", {self.method} {self.path}" if self.method and self.path else ""
        return f"{base} (status={self.status_code}{where})"


class BadRequestError(APIStatusError):
    """HTTP 400 - malformed request or a value the envelope descriptor does not allow."""


class UnauthorizedError(APIStatusError):
    """HTTP 401 - token missing, expired, or used from another IP than it was issued to."""


class ForbiddenError(APIStatusError):
    """HTTP 403 - wrong ``x-namespace`` for the resource, or the user lacks the role."""


class NotFoundError(APIStatusError):
    """HTTP 404."""


class ConflictError(APIStatusError):
    """HTTP 409 - the envelope is in a state that does not allow the change."""


class UnprocessableEntityError(APIStatusError):
    """HTTP 422 - validation failed (e.g. file type or size)."""


class RateLimitError(APIStatusError):
    """HTTP 429."""


class ServerError(APIStatusError):
    """HTTP 5xx."""


_STATUS_TO_EXC: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: UnauthorizedError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def status_error_from_response(status_code: int, *, body: Any = None,
                               method: str | None = None, path: str | None = None) -> APIStatusError:
    """Build the most specific :class:`APIStatusError` for ``status_code``."""
    exc_type = _STATUS_TO_EXC.get(status_code)
    if exc_type is None:
        exc_type = ServerError if status_code >= 500 else APIStatusError
    message = _message(body) or f"Verified API returned HTTP {status_code}"
    return exc_type(message, status_code=status_code, body=body, method=method, path=path)


def _message(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "title"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return str(value["message"])
    if isinstance(body, str) and body and len(body) <= 200:
        return body
    return None
