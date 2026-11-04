"""GET /api/fubon/status: read-only Fubon sidecar status with a mocked sidecar."""

import inspect
import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

from app.routes import fubon as fubon_route
from app.routes.openapi_brokers import openapi_brokers_blp
from app.services import broker_market_policy as policy
from app.services import fubon_status
from app.utils import trading_venue_visibility as venues

TOKEN = "test-token"
FUBON_ID = "example-id"
BASE_URL = "http://fubon-marketdata:8010"
SECRETS = (TOKEN, FUBON_ID, "example-password", "test-key", "/certs/example.pfx")

EXPECTED_KEYS = {
    "configured", "connected", "logged_in", "realtime_ready",
    "sdk_version", "capabilities", "trading", "checked_at",
}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def sidecar(monkeypatch):
    """Configured sidecar whose ``requests.get`` answer each test controls."""
    state = SimpleNamespace(calls=[], answer=None)
    monkeypatch.setattr(
        fubon_status,
        "FubonSidecarConfig",
        SimpleNamespace(BASE_URL=BASE_URL, TOKEN=TOKEN, TIMEOUT=7.5),
    )

    def fake_get(url, headers=None, timeout=None, **kwargs):
        state.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if isinstance(state.answer, Exception):
            raise state.answer
        return state.answer

    monkeypatch.setattr(fubon_status.requests, "get", fake_get)
    fubon_status.reset_cache()
    yield state
    fubon_status.reset_cache()


def _assert_no_secrets(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def test_unconfigured_sidecar_reports_not_configured_without_calling_it(sidecar, monkeypatch):
    monkeypatch.setattr(
        fubon_status,
        "FubonSidecarConfig",
        SimpleNamespace(BASE_URL=BASE_URL, TOKEN="", TIMEOUT=7.5),
    )
    status = fubon_status.get_fubon_status()
    assert status["configured"] is False
    assert status["connected"] is False
    assert status["logged_in"] is False
    assert status["trading"] is False
    assert "error" not in status
    assert sidecar.calls == []


def test_healthy_sidecar_reports_connected_and_ready(sidecar):
    sidecar.answer = FakeResponse(200, {
        "status": "ok", "logged_in": True, "realtime_ready": True, "sdk_version": "2.2.8",
    })
    status = fubon_status.get_fubon_status()
    assert set(status) == EXPECTED_KEYS
    assert status["configured"] is True
    assert status["connected"] is True
    assert status["logged_in"] is True
    assert status["realtime_ready"] is True
    assert status["sdk_version"] == "2.2.8"
    assert status["capabilities"] == ["market_data"]
    assert status["trading"] is False
    checked_at = datetime.fromisoformat(status["checked_at"])
    assert checked_at.utcoffset().total_seconds() == 8 * 3600
    assert sidecar.calls == [{
        "url": f"{BASE_URL}/health",
        "headers": {"X-Sidecar-Token": TOKEN},
        "timeout": 7.5,
    }]


def test_sidecar_not_logged_in_is_connected_but_not_logged_in(sidecar):
    sidecar.answer = FakeResponse(200, {
        "status": "starting", "logged_in": False, "realtime_ready": False, "sdk_version": None,
    })
    status = fubon_status.get_fubon_status()
    assert status["connected"] is True
    assert status["logged_in"] is False
    assert status["realtime_ready"] is False
    assert status["sdk_version"] is None
    assert "error" not in status


@pytest.mark.parametrize(
    "answer, error",
    [
        (requests.ConnectionError("refused"), "sidecarUnreachable"),
        (requests.Timeout("slow"), "sidecarTimeout"),
        (FakeResponse(401, {"code": "unauthorized"}), "sidecarUnauthorized"),
        (FakeResponse(500, {"code": "internalError"}), "sidecarError"),
        (FakeResponse(200, ValueError("not json")), "sidecarInvalidResponse"),
        (FakeResponse(200, ["not", "an", "object"]), "sidecarInvalidResponse"),
    ],
)
def test_sidecar_failures_report_disconnected_with_error_code(sidecar, answer, error):
    sidecar.answer = answer
    status = fubon_status.get_fubon_status()
    assert set(status) == EXPECTED_KEYS | {"error"}
    assert status["configured"] is True
    assert status["connected"] is False
    assert status["logged_in"] is False
    assert status["realtime_ready"] is False
    assert status["trading"] is False
    assert status["error"] == error


def test_route_returns_200_even_when_sidecar_is_down(sidecar):
    sidecar.answer = requests.ConnectionError("refused")
    app = Flask(__name__)
    with app.test_request_context("/api/fubon/status"):
        response = inspect.unwrap(fubon_route.get_status)()
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["data"]["connected"] is False
    assert body["data"]["error"] == "sidecarUnreachable"


def test_route_requires_login_and_wins_over_generic_broker_route():
    app = Flask(__name__)
    app.register_blueprint(fubon_route.fubon_blp, url_prefix="/api/fubon")
    app.register_blueprint(openapi_brokers_blp, url_prefix="/api")
    endpoint, _ = app.url_map.bind("localhost").match("/api/fubon/status", method="GET")
    assert endpoint == "fubon.get_status"
    response = app.test_client().get("/api/fubon/status")
    assert response.status_code == 401


def test_status_is_cached_for_a_short_ttl(sidecar):
    sidecar.answer = FakeResponse(200, {"logged_in": True, "realtime_ready": True, "sdk_version": "2.2.8"})
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    first = fubon_status.get_fubon_status(clock=clock)
    now[0] += fubon_status.CACHE_TTL_SECONDS - 1
    assert fubon_status.get_fubon_status(clock=clock) == first
    assert len(sidecar.calls) == 1
    now[0] += 2
    fubon_status.get_fubon_status(clock=clock)
    assert len(sidecar.calls) == 2


def test_cache_is_invalidated_when_the_token_changes(sidecar, monkeypatch):
    sidecar.answer = FakeResponse(401, {"code": "unauthorized"})
    assert fubon_status.get_fubon_status()["error"] == "sidecarUnauthorized"
    monkeypatch.setattr(
        fubon_status,
        "FubonSidecarConfig",
        SimpleNamespace(BASE_URL=BASE_URL, TOKEN="test-token-2", TIMEOUT=7.5),
    )
    sidecar.answer = FakeResponse(200, {"logged_in": True, "realtime_ready": True})
    assert fubon_status.get_fubon_status()["connected"] is True
    assert len(sidecar.calls) == 2


@pytest.mark.parametrize(
    "answer",
    [
        FakeResponse(200, {
            "logged_in": True, "realtime_ready": True, "sdk_version": "2.2.8",
            "fubon_id": FUBON_ID, "password": "example-password", "api_key": "test-key",
            "cert_path": "/certs/example.pfx", "token": TOKEN,
        }),
        FakeResponse(401, {"code": "unauthorized", "message": f"bad token {TOKEN}"}),
        requests.ConnectionError(f"{BASE_URL} token={TOKEN}"),
        requests.Timeout(f"{BASE_URL} token={TOKEN}"),
        FakeResponse(503, {"code": "notReady", "message": f"login {FUBON_ID} failed"}),
    ],
)
def test_response_and_logs_never_contain_credentials(sidecar, caplog, answer):
    sidecar.answer = answer
    with caplog.at_level(logging.DEBUG):
        status = fubon_status.get_fubon_status()
    assert set(status) <= EXPECTED_KEYS | {"error"}
    _assert_no_secrets(json.dumps(status), caplog.text)
    assert BASE_URL not in json.dumps(status)


def test_fubon_is_not_a_live_market_or_trading_venue():
    for name in ("fubon", "fubon-marketdata", "fubon_sidecar"):
        assert name not in policy.BROKER_MARKETS
        assert name not in venues.TRADING_VENUE_IDS
        assert venues.is_trading_venue_enabled(name) is False
        assert venues.is_trading_venue_enabled(name, {"ENABLED_TRADING_VENUES": name}) is False
        assert policy.is_compatible_credential(name, "TWStock") is False
    for market in ("TWStock", "TWFutures"):
        assert market not in policy.LIVE_MARKET_CATEGORIES
        assert policy.list_supported_brokers_for_market(market) == set()
    assert all("fubon" not in str(item).lower() for item in venues.trading_venue_options())
