from __future__ import annotations

import pytest

from verified_esign import Config, ConfigurationError, DEFAULT_BASE_URL


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFIED_EMAIL", "api@example.com")
    monkeypatch.setenv("VERIFIED_PASSWORD", "pw")
    monkeypatch.setenv("VERIFIED_COMPANY_ID", "/companies/ABC")
    monkeypatch.setenv("VERIFIED_IP_LOCK", "0")
    cfg = Config.from_env()
    assert cfg.company_id == "ABC" and cfg.namespace == "/companies/ABC"
    assert cfg.ip_lock is False
    assert cfg.base_url == DEFAULT_BASE_URL


def test_defaults_keep_verified_ip_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFIED_EMAIL", "a@b.se")
    monkeypatch.setenv("VERIFIED_PASSWORD", "pw")
    monkeypatch.delenv("VERIFIED_IP_LOCK", raising=False)
    monkeypatch.delenv("VERIFIED_COMPANY_ID", raising=False)
    cfg = Config.from_env()
    assert cfg.ip_lock is True and cfg.namespace is None


def test_bad_ip_lock_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFIED_EMAIL", "a@b.se")
    monkeypatch.setenv("VERIFIED_PASSWORD", "pw")
    monkeypatch.setenv("VERIFIED_IP_LOCK", "maybe")
    with pytest.raises(ConfigurationError):
        Config.from_env()


@pytest.mark.parametrize("kwargs", [{"email": "", "password": "x"}, {"email": "a@b.se", "password": ""}])
def test_missing_credentials(kwargs: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        Config(**kwargs)


def test_password_not_in_repr() -> None:
    assert "hunter2" not in repr(Config(email="a@b.se", password="hunter2"))
