from __future__ import annotations

import pytest
from conftest import TOKEN, make_settings

from fubon_sidecar.config import ConfigError, Settings


def test_token_shorter_than_32_characters_is_rejected():
    with pytest.raises(ConfigError, match="at least 32"):
        Settings.from_env({"FUBON_SIDECAR_TOKEN": "t" * 31})
    with pytest.raises(ConfigError):
        Settings.from_env({})


def test_from_env_reads_all_variables():
    settings = Settings.from_env(
        {
            "FUBON_SIDECAR_TOKEN": TOKEN,
            "FUBON_ID": "test-id",
            "FUBON_API_KEY": "test-key",
            "FUBON_CERT_PATH": "/run/fubon/test.pfx",
            "FUBON_CERT_PASSWORD": "test-cert-pass",
            "FUBON_REQUEST_TIMEOUT_SEC": "3",
            "FUBON_RATE_LIMIT_PER_SEC": "2.5",
            "FUBON_SIDECAR_PORT": "9000",
        }
    )

    assert settings.uses_api_key is True
    assert settings.missing_credentials() == []
    assert settings.request_timeout_sec == 3.0
    assert settings.rate_limit_per_sec == 2.5
    assert settings.port == 9000


def test_password_required_without_api_key():
    assert make_settings(password="").missing_credentials() == ["FUBON_PASSWORD"]
    assert make_settings(password="", api_key="test-key").missing_credentials() == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FUBON_REQUEST_TIMEOUT_SEC", "abc"),
        ("FUBON_REQUEST_TIMEOUT_SEC", "0"),
        ("FUBON_RATE_LIMIT_PER_SEC", "-1"),
        ("FUBON_SIDECAR_PORT", "70000"),
    ],
)
def test_invalid_numbers_are_rejected(key, value):
    with pytest.raises(ConfigError):
        Settings.from_env({"FUBON_SIDECAR_TOKEN": TOKEN, key: value})


def test_repr_never_contains_secrets():
    settings = make_settings(api_key="test-key")
    text = repr(settings)
    for secret in (TOKEN, "test-id", "test-password", "test-key", "test-cert-pass"):
        assert secret not in text
