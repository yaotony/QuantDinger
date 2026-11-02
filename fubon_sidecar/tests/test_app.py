from __future__ import annotations

import os

import pytest

# CI installs ".[dev]" and must run these tests; only local runs without FastAPI skip.
if not os.environ.get("CI"):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

from conftest import TOKEN, WRONG_TOKEN  # noqa: E402
from fake_sdk import FakeFugleError, FakeSDK  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fubon_sidecar.app import create_app  # noqa: E402
from fubon_sidecar.config import TOKEN_HEADER  # noqa: E402

AUTH = {TOKEN_HEADER: TOKEN}
ISO_0845 = "2026-07-06T08:45:00.000+08:00"
T_0845 = 1783298700
CANDLES = {"data": [{"date": ISO_0845, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3}]}

READ_ONLY_ROUTES = {
    "/health",
    "/v1/stock/candles",
    "/v1/stock/intraday/candles",
    "/v1/stock/quote",
    "/v1/stock/tickers",
    "/v1/futopt/intraday/candles",
    "/v1/futopt/quote",
    "/v1/futopt/tickers",
}


@pytest.fixture
def make_client(make_service):
    clients: list[TestClient] = []

    def build(sdk: FakeSDK | None = None, *, login: bool = True) -> TestClient:
        service = make_service(sdk, login=login)
        app = create_app(service._settings, service=service, start_login=False)
        client = TestClient(app)
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def test_routes_are_read_only_get_endpoints(client):
    routes = [route for route in client.app.routes if hasattr(route, "methods")]
    assert {route.path for route in routes} == READ_ONLY_ROUTES
    for route in routes:
        assert route.methods <= {"GET", "HEAD"}


@pytest.mark.parametrize("path", sorted(READ_ONLY_ROUTES) + ["/orders", "/docs", "/openapi.json"])
@pytest.mark.parametrize("headers", [{}, {TOKEN_HEADER: WRONG_TOKEN}, {TOKEN_HEADER: TOKEN[:-1]}])
def test_every_path_requires_the_sidecar_token(client, path, headers):
    response = client.get(path, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_health(client):
    response = client.get("/health", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "logged_in": True, "realtime_ready": True, "sdk_version": "2.2.8"}


def test_health_before_login(make_client):
    response = make_client(login=False).get("/health", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["logged_in"] is False


def test_docs_and_order_paths_do_not_exist(client):
    for path in ("/docs", "/openapi.json", "/orders", "/positions"):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {"code": "notFound"}
    assert client.post("/v1/stock/quote", headers=AUTH).status_code == 405


def test_stock_candles_with_range(client, fake_sdk):
    fake_sdk.responses["stock.historical.candles"] = {
        "data": [{"date": "2026-07-06", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    }

    response = client.get(
        "/v1/stock/candles",
        params={"symbol": "2330", "timeframe": "D", "from": "2026-01-01", "to": "2026-07-06"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert fake_sdk.calls == [
        (
            "stock.historical.candles",
            {"symbol": "2330", "timeframe": "D", "from": "2026-01-01", "to": "2026-07-06"},
        )
    ]
    assert response.json()["candles"] == [
        {"time": 1783267200, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    ]


def test_stock_intraday_candles_convert_date_field(client, fake_sdk):
    fake_sdk.responses["stock.intraday.candles"] = CANDLES

    response = client.get(
        "/v1/stock/intraday/candles", params={"symbol": "IX0001", "timeframe": "1"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "IX0001",
        "timeframe": "1",
        "candles": [{"time": T_0845, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}],
    }


def test_stock_quote(client, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = {"symbol": "2330", "name": "台積電", "lastPrice": 1000}

    response = client.get("/v1/stock/quote", params={"symbol": "2330"}, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["name"] == "台積電"


def test_stock_tickers(client, fake_sdk):
    fake_sdk.responses["stock.intraday.tickers"] = {"data": [{"symbol": "IX0001", "name": "發行量加權股價指數"}]}

    response = client.get("/v1/stock/tickers", params={"type": "INDEX", "exchange": "TWSE"}, headers=AUTH)

    assert response.status_code == 200
    assert fake_sdk.calls == [("stock.intraday.tickers", {"type": "INDEX", "exchange": "TWSE"})]
    assert response.json()["tickers"] == [{"symbol": "IX0001", "name": "發行量加權股價指數"}]


@pytest.mark.parametrize(
    ("session", "expected_params"),
    [
        (None, {"symbol": "TXFG6", "timeframe": "1"}),
        ("regular", {"symbol": "TXFG6", "timeframe": "1"}),
        ("afterhours", {"symbol": "TXFG6", "timeframe": "1", "session": "afterhours"}),
    ],
)
def test_futopt_intraday_candles_session(client, fake_sdk, session, expected_params):
    fake_sdk.responses["futopt.intraday.candles"] = CANDLES
    params = {"symbol": "TXFG6", "timeframe": "1"}
    if session:
        params["session"] = session

    response = client.get("/v1/futopt/intraday/candles", params=params, headers=AUTH)

    assert response.status_code == 200
    assert fake_sdk.calls == [("futopt.intraday.candles", expected_params)]
    assert response.json()["session"] == (session or "regular")
    assert response.json()["candles"][0]["time"] == T_0845


def test_futopt_quote(client, fake_sdk):
    fake_sdk.responses["futopt.intraday.quote"] = {"symbol": "TXFG6", "lastPrice": 22000, "previousClose": 21900}

    response = client.get("/v1/futopt/quote", params={"symbol": "TXFG6", "session": "afterhours"}, headers=AUTH)

    assert response.status_code == 200
    assert fake_sdk.calls == [("futopt.intraday.quote", {"symbol": "TXFG6", "session": "afterhours"})]
    assert response.json()["previousClose"] == 21900.0
    assert response.json()["session"] == "afterhours"


def test_futopt_tickers(client, fake_sdk):
    fake_sdk.responses["futopt.intraday.tickers"] = {
        "data": [{"symbol": "TXFG6", "name": "臺股期貨", "endDate": "2026-07-15", "settlementDate": "2026-07-15"}]
    }

    response = client.get("/v1/futopt/tickers", params={"type": "FUTURE"}, headers=AUTH)

    assert response.status_code == 200
    ticker = response.json()["tickers"][0]
    assert ticker["symbol"] == "TXFG6"
    assert ticker["endDate"] == "2026-07-15"
    assert ticker["settlementDate"] == "2026-07-15"


def test_not_found(client, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = FakeFugleError("Resource Not Found", status_code=404)
    fake_sdk.responses["futopt.intraday.candles"] = {"data": []}

    quote = client.get("/v1/stock/quote", params={"symbol": "9999"}, headers=AUTH)
    candles = client.get("/v1/futopt/intraday/candles", params={"symbol": "TXFG6"}, headers=AUTH)

    assert quote.status_code == 404 and quote.json()["code"] == "notFound"
    assert candles.status_code == 404 and candles.json()["code"] == "notFound"


def test_upstream_error_hides_secrets(client, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = RuntimeError(f"socket closed {TOKEN} test-password")

    response = client.get("/v1/stock/quote", params={"symbol": "2330"}, headers=AUTH)

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "upstreamError"
    assert TOKEN not in response.text and "test-password" not in response.text
    assert body["message"] == "Fubon market data request failed (RuntimeError)"


def test_not_ready_before_login(make_client, fake_sdk):
    response = make_client(login=False).get("/v1/stock/quote", params={"symbol": "2330"}, headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "notReady"
    assert fake_sdk.calls == []


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/stock/candles", {"symbol": "2330"}),
        ("/v1/stock/candles", {"symbol": "2330", "timeframe": "5", "from": "2026-07-01"}),
        ("/v1/stock/candles", {"symbol": "2330", "timeframe": "D", "from": "2024-01-01", "to": "2026-01-01"}),
        ("/v1/stock/quote", {}),
        ("/v1/stock/tickers", {}),
        ("/v1/futopt/intraday/candles", {"symbol": "TXFG6", "session": "night"}),
        ("/v1/futopt/tickers", {"type": "SPOT"}),
    ],
)
def test_invalid_requests_are_400(client, fake_sdk, path, params):
    response = client.get(path, params=params, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["code"] == "invalidRequest"
    assert fake_sdk.calls == []
