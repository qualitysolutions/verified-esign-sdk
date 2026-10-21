"""Login, token caching, namespace handling, retries and error mapping."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx
import pytest

from verified_esign import (
    AuthenticationError,
    Config,
    ConfigurationError,
    ForbiddenError,
    NotFoundError,
    ServerError,
    VerifiedClient,
)
from verified_esign._auth import jwt_expiry

from .conftest import BASE, NAMESPACE, live_response, make_jwt, mock_login


def test_login_uses_basic_auth_and_jwt_header(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(200, json={"id": "E1"}))
    client.envelopes.get("E1")
    login = respx_mock.calls[0].request
    assert login.headers["authorization"] == "Basic " + base64.b64encode(b"api@example.com:s3cr3t").decode()
    req = route.calls.last.request
    assert req.headers["authorization"].startswith("JWT ")
    assert req.headers["x-namespace"] == NAMESPACE


def test_token_is_cached(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(200, json={"id": "E1"}))
    client.envelopes.get("E1")
    client.envelopes.get("E1")
    assert sum(1 for c in respx_mock.calls if c.request.url.path.endswith("/auth")) == 1


def test_expired_token_triggers_new_login(config: Config, respx_mock: Any) -> None:
    login = mock_login(respx_mock, token=make_jwt(time.time() + 60))  # inside the 1h leeway
    respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(200, json={"id": "E1"}))
    with VerifiedClient(config) as c:
        c.envelopes.get("E1")
        c.envelopes.get("E1")
    assert login.call_count == 2


def test_401_logs_in_again_once(client: VerifiedClient, respx_mock: Any) -> None:
    route = respx_mock.get(f"{BASE}/envelopes/E1").mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json={"id": "E1"})])
    assert client.envelopes.get("E1").id == "E1"
    assert route.call_count == 2


def test_without_ip_lock(respx_mock: Any) -> None:
    login = mock_login(respx_mock, ip_lock=False)
    respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(200, json={"id": "E1"}))
    cfg = Config(email="a@b.se", password="pw", company_id="COMPANY1", base_url=BASE, ip_lock=False)
    with VerifiedClient(cfg) as c:
        c.envelopes.get("E1")
    assert login.called


def test_failed_login_does_not_echo_body(config: Config, respx_mock: Any) -> None:
    respx_mock.post(f"{BASE}/auth").mock(return_value=httpx.Response(401, text="secret-ish detail"))
    with VerifiedClient(config) as c, pytest.raises(AuthenticationError) as info:
        c.envelopes.get("E1")
    assert info.value.status_code == 401 and "secret-ish" not in str(info.value)


def test_namespace_resolved_from_userinfo(respx_mock: Any) -> None:
    mock_login(respx_mock)
    respx_mock.get(f"{BASE}/auth/userinfo").mock(return_value=live_response("get_auth_userinfo.json"))
    route = respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(200, json={"id": "E1"}))
    with VerifiedClient(Config(email="a@b.se", password="pw", base_url=BASE)) as c:
        c.envelopes.get("E1")
        assert c.namespace == "/companies/COMPANY1"
    assert route.calls.last.request.headers["x-namespace"] == "/companies/COMPANY1"


def test_ambiguous_company_needs_company_id(respx_mock: Any) -> None:
    mock_login(respx_mock)
    respx_mock.get(f"{BASE}/auth/userinfo").mock(return_value=httpx.Response(200, json={"id": "U", "roles": [
        {"name": "/roles/admin/companies/A"}, {"name": "/roles/user/companies/B"}]}))
    with VerifiedClient(Config(email="a@b.se", password="pw", base_url=BASE)) as c, \
            pytest.raises(ConfigurationError, match="2 companies"):
        c.envelopes.get("E1")


def test_userinfo_model(client: VerifiedClient, respx_mock: Any) -> None:
    respx_mock.get(f"{BASE}/auth/userinfo").mock(return_value=live_response("get_auth_userinfo.json"))
    info = client.userinfo()
    assert info.company_ids == ["COMPANY1"] and info.given_name == "Test"


@pytest.mark.parametrize("status,exc", [(403, ForbiddenError), (404, NotFoundError), (502, ServerError)])
def test_error_mapping(client: VerifiedClient, respx_mock: Any, status: int, exc: type[Exception]) -> None:
    respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(status, json={"message": "nope"}))
    with pytest.raises(exc, match="nope"):
        client.envelopes.get("E1")


def test_get_is_retried_on_503(config: Config, respx_mock: Any) -> None:
    mock_login(respx_mock)
    route = respx_mock.get(f"{BASE}/envelopes/E1").mock(side_effect=[
        httpx.Response(503, headers={"retry-after": "0"}), httpx.Response(200, json={"id": "E1"})])
    cfg = Config(email="a@b.se", password="pw", company_id="C", base_url=BASE, max_retries=2)
    with VerifiedClient(cfg) as c:
        assert c.envelopes.get("E1").id == "E1"
    assert route.call_count == 2


def test_post_is_not_retried_on_503(config: Config, respx_mock: Any) -> None:
    """No idempotency keys at Verified: a replayed POST could create a second envelope."""
    mock_login(respx_mock)
    route = respx_mock.post(f"{BASE}/envelope-descriptors/default/envelopes").mock(
        return_value=httpx.Response(503, headers={"retry-after": "0"}))
    cfg = Config(email="a@b.se", password="pw", company_id="C", base_url=BASE, max_retries=2)
    with VerifiedClient(cfg) as c, pytest.raises(ServerError):
        c.envelopes.create()
    assert route.call_count == 1


def test_jwt_expiry() -> None:
    assert jwt_expiry(make_jwt(1_900_000_000)) == 1_900_000_000
    assert jwt_expiry("not-a-jwt") is None


def test_logs_never_contain_token_or_password(client: VerifiedClient, respx_mock: Any,
                                               caplog: pytest.LogCaptureFixture) -> None:
    respx_mock.get(f"{BASE}/envelopes/E1").mock(return_value=httpx.Response(200, json={"id": "E1"}))
    with caplog.at_level(logging.DEBUG, logger="verified_esign"):
        client.envelopes.get("E1")
    text = caplog.text
    assert "s3cr3t" not in text and "eyJ" not in text and "Basic " not in text
