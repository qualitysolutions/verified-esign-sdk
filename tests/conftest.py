from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from verified_esign import AsyncVerifiedClient, Config, VerifiedClient

BASE = "https://verified.test/api"
NAMESPACE = "/companies/COMPANY1"
FIXTURES = Path(__file__).parent / "fixtures" / "live"
PDF = b"%PDF-1.4\n%test\n"


def make_jwt(exp: float | None = None) -> str:
    """An unsigned JWT-shaped token (the SDK only reads ``exp``)."""
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    claims: dict[str, Any] = {"sub": "/users/USER1"}
    if exp is not None:
        claims["exp"] = int(exp)
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.c2ln"


def live(name: str) -> dict[str, Any]:
    """A sanitized recorded response: {"request", "status", "location", "body"}."""
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def live_response(name: str) -> httpx.Response:
    rec = live(name)
    headers = {"location": rec["location"]} if rec["location"] else {}
    if rec["body"] is None:
        return httpx.Response(rec["status"], headers=headers)
    return httpx.Response(rec["status"], json=rec["body"], headers=headers)


def mock_login(respx_mock: Any, *, token: str | None = None, ip_lock: bool = True) -> Any:
    url = f"{BASE}/auth" if ip_lock else f"{BASE}/auth?withoutIpLock=1"
    return respx_mock.post(url).mock(
        return_value=httpx.Response(200, json={"token": token or make_jwt(time.time() + 7 * 86400)}))


@pytest.fixture
def config() -> Config:
    return Config(email="api@example.com", password="s3cr3t", company_id="COMPANY1",
                  base_url=BASE, max_retries=0)


@pytest.fixture
def client(config: Config, respx_mock: Any) -> Any:
    mock_login(respx_mock)
    c = VerifiedClient(config)
    yield c
    c.close()


@pytest.fixture
async def async_client(config: Config, respx_mock: Any) -> Any:
    mock_login(respx_mock)
    c = AsyncVerifiedClient(config)
    yield c
    await c.aclose()
