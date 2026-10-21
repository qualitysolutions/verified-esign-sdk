"""Login and token handling.

``POST {base_url}/auth`` with ``Authorization: Basic base64(email:password)`` returns
``{"token": "<JWT>"}``. Observed live (2026-10-21): the JWT is RS256-signed (the docs show
HS256) and lives 7 days. The SDK reads ``exp`` from the JWT payload, without verifying the
signature, only to know when to log in again. ``GET /auth/token`` renews a token without
the password; renewal does not invalidate the old token.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config
from .errors import AuthenticationError

logger = logging.getLogger("verified_esign")

#: Documented token lifetime, used when the JWT has no readable ``exp``.
TOKEN_LIFETIME = 7 * 24 * 3600.0


@dataclass(frozen=True)
class AccessToken:
    """A cached JWT and when it becomes stale (leeway applied)."""

    value: str
    #: Epoch seconds after which the token should be replaced.
    expires_at: float

    def is_valid(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.expires_at


def jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a JWT (epoch seconds), or ``None`` if it cannot be read."""
    try:
        payload = token.split(".")[1]
        claims: Any = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None


class PasswordLogin:
    """Builds login/renew requests and parses tokens. Stateless; the transports own the cache."""

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def login_url(self) -> str:
        suffix = "" if self._config.ip_lock else "?withoutIpLock=1"
        return f"{self._config.base_url}/auth{suffix}"

    @property
    def renew_url(self) -> str:
        return f"{self._config.base_url}/auth/token"

    def _basic(self) -> dict[str, str]:
        raw = f"{self._config.email}:{self._config.password}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}", "Accept": "application/json"}

    def login_sync(self, client: httpx.Client) -> AccessToken:
        try:
            response = client.post(self.login_url, headers=self._basic())
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Failed to reach the login endpoint: {exc}") from exc
        return self._parse(response)

    async def login_async(self, client: httpx.AsyncClient) -> AccessToken:
        try:
            response = await client.post(self.login_url, headers=self._basic())
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Failed to reach the login endpoint: {exc}") from exc
        return self._parse(response)

    def renew_sync(self, client: httpx.Client, token: str) -> AccessToken:
        try:
            response = client.get(self.renew_url, headers={"Authorization": f"JWT {token}",
                                                           "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Failed to reach the token renewal endpoint: {exc}") from exc
        return self._parse(response)

    async def renew_async(self, client: httpx.AsyncClient, token: str) -> AccessToken:
        try:
            response = await client.get(self.renew_url, headers={"Authorization": f"JWT {token}",
                                                                 "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Failed to reach the token renewal endpoint: {exc}") from exc
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> AccessToken:
        if response.status_code != 200:
            # Never echo the body or request: it is a credential exchange.
            raise AuthenticationError(
                f"Verified rejected the login (HTTP {response.status_code}); check the email, "
                "password and, for 401 on a renewal, that the IP did not change",
                status_code=response.status_code,
            )
        try:
            data: Any = response.json()
        except ValueError as exc:
            raise AuthenticationError("Login response was not JSON") from exc
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AuthenticationError("Login response did not contain a token")
        exp = jwt_expiry(token) or time.time() + TOKEN_LIFETIME
        logger.debug("obtained Verified token (valid %ds)", int(exp - time.time()))
        return AccessToken(value=token, expires_at=exp - self._config.token_expiry_leeway)
