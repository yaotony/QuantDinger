from __future__ import annotations

from typing import Any

import pytest
from fake_sdk import FakeSDK, factory_for

from fubon_sidecar.config import Settings
from fubon_sidecar.service import FubonMarketData

# Deliberately low-entropy so secret scanners do not mistake them for real values.
TOKEN = "t" * 32
WRONG_TOKEN = "w" * 32
CREDENTIALS = {
    "fubon_id": "test-id",
    "password": "test-password",
    "cert_path": "/certs/test.pfx",
    "cert_password": "test-cert-pass",
}


def make_settings(**overrides: Any) -> Settings:
    # A high default rate keeps tests from sleeping; rate-limit tests override it.
    values: dict[str, Any] = {"sidecar_token": TOKEN, "rate_limit_per_sec": 1000.0, **CREDENTIALS}
    values.update(overrides)
    return Settings(**values)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_sdk() -> FakeSDK:
    return FakeSDK()


@pytest.fixture
def make_service(fake_sdk: FakeSDK):
    services: list[FubonMarketData] = []

    def build(sdk: FakeSDK | None = None, *, login: bool = True, **kwargs: Any) -> FubonMarketData:
        settings_overrides = kwargs.pop("settings", {})
        kwargs.setdefault("cert_exists", lambda _path: True)
        kwargs.setdefault("version_provider", lambda: "2.2.8")
        service = FubonMarketData(
            make_settings(**settings_overrides), factory_for(sdk or fake_sdk), **kwargs
        )
        services.append(service)
        if login:
            assert service.login() is True
        return service

    yield build
    for service in services:
        service.close()


@pytest.fixture
def service(make_service) -> FubonMarketData:
    return make_service()
