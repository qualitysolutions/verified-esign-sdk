"""Configuration for the Verified eSign SDK client."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .errors import ConfigurationError

DEFAULT_BASE_URL = "https://app.verified.eu/api"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass
class Config:
    """Configuration for a Verified client.

    Verified has no API keys: the SDK logs in as a Verified **user** (``email`` +
    ``password``) and receives a JWT valid for 7 days. Use a dedicated API user, not a
    personal login, and keep its password in a secret store.

    ``company_id`` selects the company the SDK acts for (sent as
    ``x-namespace: /companies/<company_id>``). When ``None`` it is read from
    ``/auth/userinfo`` on first use, which only works if the user belongs to exactly one
    company. Envelopes are bound to the namespace they were created in.

    ``ip_lock`` mirrors Verified's default: a token only works from the IP that logged
    in. Set ``False`` (``POST /auth?withoutIpLock=1``) where the egress IP can change.

    :meth:`from_env` reads ``VERIFIED_*`` variables (see its docstring).
    """

    email: str
    password: str = field(repr=False)
    company_id: str | None = None
    base_url: str = DEFAULT_BASE_URL
    ip_lock: bool = True

    # Networking.
    timeout: float = 30.0
    #: Retries for transient failures. Only idempotent requests (GET/PUT/DELETE) are
    #: retried on 429/5xx; POSTs are only retried when the connection never opened,
    #: because Verified has no idempotency keys and a replayed POST creates a duplicate.
    max_retries: int = 2
    #: Longest ``Retry-After`` (seconds) the SDK will sleep for on 429/503.
    max_retry_after: float = 30.0
    verify_tls: bool = True
    #: Seconds before the token's ``exp`` at which the SDK logs in again.
    token_expiry_leeway: float = 3600.0
    #: Optional extra headers applied to every API request.
    default_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.email:
            raise ConfigurationError("email is required (VERIFIED_EMAIL)")
        if not self.password:
            raise ConfigurationError("password is required (VERIFIED_PASSWORD)")
        if not self.base_url:
            raise ConfigurationError("base_url must not be empty")
        self.base_url = self.base_url.rstrip("/")
        if self.company_id:
            # Accept "/companies/<id>" or the bare id.
            self.company_id = self.company_id.rstrip("/").rsplit("/", 1)[-1]

    @property
    def namespace(self) -> str | None:
        return f"/companies/{self.company_id}" if self.company_id else None

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        """Build a :class:`Config` from environment variables.

        ``VERIFIED_EMAIL``, ``VERIFIED_PASSWORD`` (required), ``VERIFIED_COMPANY_ID``,
        ``VERIFIED_IP_LOCK`` (``1``/``0``, default ``1``) and ``VERIFIED_BASE_URL``.
        Keyword overrides win over the environment.
        """
        ip_lock_raw = (os.getenv("VERIFIED_IP_LOCK") or "").strip().lower()
        if ip_lock_raw and ip_lock_raw not in _TRUE | _FALSE:
            raise ConfigurationError(f"VERIFIED_IP_LOCK must be 1 or 0, got {ip_lock_raw!r}")
        values: dict[str, object] = {
            "email": os.getenv("VERIFIED_EMAIL") or "",
            "password": os.getenv("VERIFIED_PASSWORD") or "",
            "company_id": os.getenv("VERIFIED_COMPANY_ID") or None,
            "base_url": os.getenv("VERIFIED_BASE_URL") or DEFAULT_BASE_URL,
            "ip_lock": ip_lock_raw not in _FALSE,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]
