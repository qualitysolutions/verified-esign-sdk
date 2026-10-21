"""Low-level HTTP transport shared by the sync and async clients.

Responsibilities:
  * log in and cache the JWT (logging in again before ``exp``, and once on a 401),
  * resolve and send the company ``x-namespace`` header,
  * retry transient failures - but only where a replay is safe: Verified has no
    idempotency keys, so a POST is only retried when the connection never opened,
  * map non-2xx responses to the SDK exception hierarchy.

The sync and async send loops are intentionally duplicated because httpx's sync and
async APIs are not interchangeable; everything else is shared free functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from ._auth import AccessToken, PasswordLogin
from .config import Config
from .errors import APIConnectionError, APITimeoutError, ConfigurationError, status_error_from_response

logger = logging.getLogger("verified_esign")

_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: A multipart file part: (filename, content, content type).
FilePart = tuple[str, bytes, str]


@dataclass(frozen=True)
class Response:
    """A successful (2xx) response: parsed body plus the headers the SDK needs."""

    status_code: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def location(self) -> str | None:
        """The ``Location`` header. Verified returns the id of created resources ONLY
        here for documents and recipients (their response body is empty)."""
        return self.headers.get("location")


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _handle_response(method: str, path: str, response: httpx.Response) -> Response:
    if 200 <= response.status_code < 300:
        return Response(status_code=response.status_code, body=_parse_body(response),
                        headers={k.lower(): v for k, v in response.headers.items()})
    raise status_error_from_response(response.status_code, body=_parse_body(response),
                                     method=method, path=path.split("?")[0])


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """Exponential backoff, or the server's ``Retry-After`` when present."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
    return float(min(2.0**attempt * 0.5, 8.0))


def _should_retry_status(config: Config, method: str, attempt: int, response: httpx.Response) -> float | None:
    """Return the delay before retrying ``response``, or None to stop and raise."""
    if method not in _IDEMPOTENT or response.status_code not in _RETRY_STATUSES or attempt >= config.max_retries:
        return None
    delay = _retry_delay(attempt, response)
    if delay > config.max_retry_after:
        logger.warning("server asked to retry after %.0fs (> max_retry_after=%.0fs); not retrying",
                       delay, config.max_retry_after)
        return None
    return delay


def _can_retry_exception(config: Config, method: str, attempt: int, exc: httpx.HTTPError) -> bool:
    if attempt >= config.max_retries:
        return False
    # A POST that never reached the server is safe to resend; one that may have is not.
    return method in _IDEMPOTENT or isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _log_response(method: str, url: str, response: httpx.Response, started: float) -> None:
    """Log the outcome at DEBUG (no bodies/headers -> no secrets/PII)."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s %s -> %d (%.0f ms)", method, url, response.status_code,
                     (time.perf_counter() - started) * 1000)


def _build_url(config: Config, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return f"{config.base_url}/{path.lstrip('/')}"


def _prepare(config: Config, json_body: Any, headers: Mapping[str, str] | None,
             files: Mapping[str, FilePart] | None) -> tuple[bytes | None, dict[str, str]]:
    """Encode a JSON body and build the per-call headers (minus Authorization/namespace)."""
    out: dict[str, str] = {"Accept": "application/json"}
    out.update(config.default_headers)
    if headers:
        out.update(headers)
    content: bytes | None = None
    if json_body is not None and files is None:
        content = json.dumps(json_body, separators=(",", ":")).encode()
        out["Content-Type"] = "application/json"
    return content, out


def namespace_from_userinfo(body: Any) -> str:
    """Pick the single company namespace out of a ``/auth/userinfo`` body."""
    roles = body.get("roles") if isinstance(body, dict) else None
    companies: list[str] = sorted({
        "/companies/" + str(r["name"]).rsplit("/companies/", 1)[1].split("/")[0]
        for r in roles or []
        if isinstance(r, dict) and isinstance(r.get("name"), str) and "/companies/" in r["name"]
    })
    if len(companies) != 1:
        raise ConfigurationError(
            f"The Verified user belongs to {len(companies)} companies {companies}; "
            "set company_id (VERIFIED_COMPANY_ID) explicitly"
        )
    return companies[0]


class SyncTransport:
    """Synchronous transport backed by :class:`httpx.Client`."""

    def __init__(self, config: Config, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._auth = PasswordLogin(config)
        self._token: AccessToken | None = None
        self._namespace = config.namespace
        self._lock = threading.Lock()
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=config.timeout, verify=config.verify_tls)

    def _get_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or self._token is None or not self._token.is_valid():
                self._token = self._auth.login_sync(self._client)
            return self._token.value

    def renew_token(self) -> str:
        """Swap the cached JWT for a renewed one via ``GET /auth/token`` and return it."""
        with self._lock:
            current = self._token.value if self._token else None
        if current is None:
            return self._get_token()
        renewed = self._auth.renew_sync(self._client, current)
        with self._lock:
            self._token = renewed
        return renewed.value

    def get_namespace(self) -> str:
        if self._namespace is None:
            body = self.request("GET", "/auth/userinfo", namespace=False).body
            self._namespace = namespace_from_userinfo(body)
            logger.debug("resolved company namespace %s from userinfo", self._namespace)
        return self._namespace

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        files: Mapping[str, FilePart] | None = None,
        data: Mapping[str, str] | None = None,
        namespace: bool = True,
        follow_redirects: bool = False,
    ) -> Response:
        method = method.upper()
        url = _build_url(self._config, path)
        content, base_headers = _prepare(self._config, json, headers, files)
        if namespace:
            base_headers["x-namespace"] = self.get_namespace()
        attempt = 0
        token_retried = False
        while True:
            request_headers = {**base_headers, "Authorization": f"JWT {self._get_token()}"}
            started = time.perf_counter()
            try:
                response = self._client.request(
                    method, url, content=content, params=params, headers=request_headers,
                    files=files, data=data, follow_redirects=follow_redirects,
                )
            except httpx.HTTPError as exc:
                if _can_retry_exception(self._config, method, attempt, exc):
                    logger.warning("%s on %s %s; retrying (%d/%d)", type(exc).__name__, method, url,
                                   attempt + 1, self._config.max_retries)
                    time.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise APITimeoutError(f"Request to {url} timed out: {exc}") from exc
                raise APIConnectionError(f"Request to {url} failed: {exc}") from exc

            _log_response(method, url, response, started)
            if response.status_code == 401 and not token_retried:
                logger.debug("401 on %s %s; logging in again and retrying once", method, url)
                token_retried = True
                self._get_token(force_refresh=True)
                continue
            delay = _should_retry_status(self._config, method, attempt, response)
            if delay is not None:
                logger.warning("%s %s returned %d; retrying in %.1fs (%d/%d)", method, url,
                               response.status_code, delay, attempt + 1, self._config.max_retries)
                time.sleep(delay)
                attempt += 1
                continue
            return _handle_response(method, path, response)

    def fetch_presigned(self, url: str) -> bytes:
        """GET a pre-signed storage URL. Sends NO Authorization header: the storage
        provider (S3) uses that header itself and rejects a Verified JWT."""
        try:
            response = self._client.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise APIConnectionError(f"Download failed: {exc}") from exc
        if response.status_code != 200:
            raise status_error_from_response(response.status_code, body=None, method="GET",
                                             path="<pre-signed file URL>")
        return response.content

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class AsyncTransport:
    """Asynchronous transport backed by :class:`httpx.AsyncClient`."""

    def __init__(self, config: Config, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._auth = PasswordLogin(config)
        self._token: AccessToken | None = None
        self._namespace = config.namespace
        self._lock = asyncio.Lock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout, verify=config.verify_tls)

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            if force_refresh or self._token is None or not self._token.is_valid():
                self._token = await self._auth.login_async(self._client)
            return self._token.value

    async def renew_token(self) -> str:
        """Swap the cached JWT for a renewed one via ``GET /auth/token`` and return it."""
        current = self._token.value if self._token else None
        if current is None:
            return await self._get_token()
        renewed = await self._auth.renew_async(self._client, current)
        async with self._lock:
            self._token = renewed
        return renewed.value

    async def get_namespace(self) -> str:
        if self._namespace is None:
            body = (await self.request("GET", "/auth/userinfo", namespace=False)).body
            self._namespace = namespace_from_userinfo(body)
            logger.debug("resolved company namespace %s from userinfo", self._namespace)
        return self._namespace

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        files: Mapping[str, FilePart] | None = None,
        data: Mapping[str, str] | None = None,
        namespace: bool = True,
        follow_redirects: bool = False,
    ) -> Response:
        method = method.upper()
        url = _build_url(self._config, path)
        content, base_headers = _prepare(self._config, json, headers, files)
        if namespace:
            base_headers["x-namespace"] = await self.get_namespace()
        attempt = 0
        token_retried = False
        while True:
            request_headers = {**base_headers, "Authorization": f"JWT {await self._get_token()}"}
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method, url, content=content, params=params, headers=request_headers,
                    files=files, data=data, follow_redirects=follow_redirects,
                )
            except httpx.HTTPError as exc:
                if _can_retry_exception(self._config, method, attempt, exc):
                    logger.warning("%s on %s %s; retrying (%d/%d)", type(exc).__name__, method, url,
                                   attempt + 1, self._config.max_retries)
                    await asyncio.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise APITimeoutError(f"Request to {url} timed out: {exc}") from exc
                raise APIConnectionError(f"Request to {url} failed: {exc}") from exc

            _log_response(method, url, response, started)
            if response.status_code == 401 and not token_retried:
                logger.debug("401 on %s %s; logging in again and retrying once", method, url)
                token_retried = True
                await self._get_token(force_refresh=True)
                continue
            delay = _should_retry_status(self._config, method, attempt, response)
            if delay is not None:
                logger.warning("%s %s returned %d; retrying in %.1fs (%d/%d)", method, url,
                               response.status_code, delay, attempt + 1, self._config.max_retries)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            return _handle_response(method, path, response)

    async def fetch_presigned(self, url: str) -> bytes:
        """GET a pre-signed storage URL without the Verified Authorization header."""
        try:
            response = await self._client.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise APIConnectionError(f"Download failed: {exc}") from exc
        if response.status_code != 200:
            raise status_error_from_response(response.status_code, body=None, method="GET",
                                             path="<pre-signed file URL>")
        return response.content

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
