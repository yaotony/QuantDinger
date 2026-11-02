from __future__ import annotations

import logging
import threading
import time

import pytest
from conftest import CREDENTIALS, TOKEN, FakeClock
from fake_sdk import FakeFugleError, FakeLoginResult, FakeSDK, sequence

from fubon_sidecar import service as service_module
from fubon_sidecar.errors import (
    InvalidRequestError,
    NotFoundError,
    NotReadyError,
    RateLimitedError,
    UpstreamError,
)
from fubon_sidecar.service import FubonMarketData, RateLimiter

ISO_0845 = "2026-07-06T08:45:00.000+08:00"
ISO_0846 = "2026-07-06T08:46:00.000+08:00"
T_0845 = 1783298700


def _candles(*dates: str) -> dict:
    return {"data": [{"date": d, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3} for d in dates]}


# --------------------------------------------------------------------------- login


def test_password_login_then_init_realtime(make_service, fake_sdk):
    service = make_service()

    assert fake_sdk.login_calls == [
        ("login", ("test-id", "test-password", "/certs/test.pfx", "test-cert-pass"))
    ]
    assert fake_sdk.realtime_inits == 1
    assert service.health() == {
        "status": "ok",
        "logged_in": True,
        "realtime_ready": True,
        "sdk_version": "2.2.8",
    }


def test_api_key_login_is_used_when_key_present_and_password_optional(make_service, fake_sdk):
    make_service(settings={"api_key": "test-key", "password": ""})

    assert fake_sdk.login_calls == [
        ("apikey_login", ("test-id", "test-key", "/certs/test.pfx", "test-cert-pass"))
    ]


def test_missing_credentials_is_fatal_and_never_calls_sdk(make_service, fake_sdk, caplog):
    service = make_service(login=False, settings={"password": ""})

    with caplog.at_level(logging.ERROR, logger="fubon_sidecar"):
        assert service.login() is False

    assert fake_sdk.login_calls == []
    assert service.health()["status"] == "error"
    assert "FUBON_PASSWORD" in caplog.text


def test_missing_certificate_is_fatal(make_service, fake_sdk):
    service = make_service(login=False, cert_exists=lambda _path: False)

    assert service.login() is False
    assert fake_sdk.login_calls == []
    assert service.health()["status"] == "error"


def test_rejected_login_is_not_retried_and_secrets_are_redacted(make_service, caplog):
    sdk = FakeSDK(login_result=FakeLoginResult(is_success=False, message="bad password test-password"))
    service = make_service(sdk, login=False)

    with caplog.at_level(logging.INFO, logger="fubon_sidecar"):
        service.run_login_loop()

    assert len(sdk.login_calls) == 1
    health = service.health()
    assert health["logged_in"] is False and health["status"] == "error"
    for secret in (*CREDENTIALS.values(), TOKEN):
        if secret != CREDENTIALS["cert_path"]:
            assert secret not in caplog.text
    assert "***" in caplog.text


def test_transient_login_errors_are_retried_with_backoff(make_service):
    attempts: list[int] = []
    sdk = FakeSDK()

    def flaky_factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("network down")
        return sdk

    service = make_service(sdk, login=False)
    service._sdk_factory = flaky_factory
    waits: list[float] = []
    service._stop.wait = lambda delay: waits.append(delay) or False  # type: ignore[method-assign]

    service.run_login_loop()

    assert len(attempts) == 3
    assert waits == list(service_module.LOGIN_RETRY_DELAYS_SEC[:2])
    assert service.health()["status"] == "ok"


def test_realtime_failure_keeps_service_not_ready(make_service, caplog):
    sdk = FakeSDK(realtime_error=RuntimeError("token exchange failed"))
    service = make_service(sdk, login=False)

    with pytest.raises(RuntimeError):
        service.login()

    health = service.health()
    assert health == {"status": "starting", "logged_in": True, "realtime_ready": False, "sdk_version": "2.2.8"}
    with pytest.raises(NotReadyError):
        service.stock_quote("2330")


def test_calls_before_login_are_not_ready(make_service, fake_sdk):
    service = make_service(login=False)

    with pytest.raises(NotReadyError):
        service.stock_candles("2330", "D")
    assert fake_sdk.calls == []


def test_start_runs_login_in_background(make_service, fake_sdk):
    service = make_service(login=False)

    thread = service.start()
    thread.join(timeout=5)

    assert service.health()["logged_in"] is True


def test_service_exposes_no_trading_capabilities(service):
    public = {name for name in dir(service) if not name.startswith("_")}
    assert public == {
        "close",
        "futopt_intraday_candles",
        "futopt_quote",
        "futopt_tickers",
        "health",
        "login",
        "run_login_loop",
        "start",
        "stock_candles",
        "stock_intraday_candles",
        "stock_quote",
        "stock_tickers",
    }


# --------------------------------------------------------------------------- stock


def test_stock_daily_candles_pass_range_and_sort_ascending(service, fake_sdk):
    fake_sdk.responses["stock.historical.candles"] = {
        "symbol": "2330",
        "data": [
            {"date": "2026-07-07", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
            {"date": "2026-07-06", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ],
    }

    body = service.stock_candles("2330", "D", "2026-01-01", "2026-07-07")

    assert fake_sdk.calls == [
        (
            "stock.historical.candles",
            {"symbol": "2330", "timeframe": "D", "from": "2026-01-01", "to": "2026-07-07"},
        )
    ]
    assert body["symbol"] == "2330" and body["timeframe"] == "D"
    assert [c["time"] for c in body["candles"]] == [1783267200, 1783353600]


def test_stock_candles_accept_unix_seconds_for_range(service, fake_sdk):
    fake_sdk.responses["stock.historical.candles"] = _candles("2026-07-06")

    service.stock_candles("2330", "W", "1783267200", "1783353600")

    assert fake_sdk.calls[0][1]["from"] == "2026-07-06"
    assert fake_sdk.calls[0][1]["to"] == "2026-07-07"


def test_stock_candles_reject_range_over_one_year(service, fake_sdk):
    with pytest.raises(InvalidRequestError):
        service.stock_candles("2330", "D", "2024-01-01", "2026-01-01")
    with pytest.raises(InvalidRequestError):
        service.stock_candles("2330", "D", "2026-02-01", "2026-01-01")
    assert fake_sdk.calls == []


def test_stock_minute_candles_cannot_take_a_range(service, fake_sdk):
    with pytest.raises(InvalidRequestError):
        service.stock_candles("2330", "5", "2026-07-01", "2026-07-06")

    fake_sdk.responses["stock.historical.candles"] = _candles(ISO_0846, ISO_0845)
    body = service.stock_candles("2330", "5")

    assert fake_sdk.calls == [("stock.historical.candles", {"symbol": "2330", "timeframe": "5"})]
    assert [c["time"] for c in body["candles"]] == [T_0845, T_0845 + 60]


@pytest.mark.parametrize("timeframe", ["2", "1m", "", "H"])
def test_invalid_timeframe_is_rejected(service, timeframe):
    with pytest.raises(InvalidRequestError):
        service.stock_candles("2330", timeframe)


@pytest.mark.parametrize("symbol", ["", "a b", "x" * 40, "../etc"])
def test_invalid_symbol_is_rejected(service, symbol):
    with pytest.raises(InvalidRequestError):
        service.stock_quote(symbol)


def test_stock_intraday_index_candles(service, fake_sdk):
    fake_sdk.responses["stock.intraday.candles"] = _candles(ISO_0845)

    body = service.stock_intraday_candles("IX0001", "1")

    assert fake_sdk.calls == [("stock.intraday.candles", {"symbol": "IX0001", "timeframe": "1"})]
    assert body == {
        "symbol": "IX0001",
        "timeframe": "1",
        "candles": [{"time": T_0845, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}],
    }


def test_stock_quote(service, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = {
        "symbol": "2330",
        "name": "台積電",
        "lastPrice": 1000,
        "previousClose": 990,
        "lastUpdated": T_0845 * 1_000_000,
    }

    quote = service.stock_quote("2330")

    assert quote["lastPrice"] == 1000.0
    assert quote["previousClose"] == 990.0
    assert quote["time"] == T_0845


def test_stock_tickers(service, fake_sdk):
    fake_sdk.responses["stock.intraday.tickers"] = {"data": [{"symbol": "2330", "name": "台積電"}]}

    body = service.stock_tickers("EQUITY", "TWSE")

    assert fake_sdk.calls == [("stock.intraday.tickers", {"type": "EQUITY", "exchange": "TWSE"})]
    assert body == {"type": "EQUITY", "exchange": "TWSE", "tickers": [{"symbol": "2330", "name": "台積電"}]}


def test_stock_tickers_validate_type_and_exchange(service):
    with pytest.raises(InvalidRequestError):
        service.stock_tickers("BOND")
    with pytest.raises(InvalidRequestError):
        service.stock_tickers("EQUITY", "NYSE")


# --------------------------------------------------------------------------- futopt


def test_futopt_regular_session_omits_session_param(service, fake_sdk):
    fake_sdk.responses["futopt.intraday.candles"] = _candles(ISO_0845)

    body = service.futopt_intraday_candles("TXFG6", "1", "regular")

    assert fake_sdk.calls == [("futopt.intraday.candles", {"symbol": "TXFG6", "timeframe": "1"})]
    assert body["session"] == "regular"


def test_futopt_afterhours_session_is_forwarded(service, fake_sdk):
    fake_sdk.responses["futopt.intraday.candles"] = _candles("2026-07-06T15:00:00.000+08:00")

    body = service.futopt_intraday_candles("TXFG6", "5", "afterhours")

    assert fake_sdk.calls == [
        ("futopt.intraday.candles", {"symbol": "TXFG6", "timeframe": "5", "session": "afterhours"})
    ]
    assert body["session"] == "afterhours"
    assert body["candles"][0]["time"] == T_0845 + 6 * 3600 + 15 * 60


def test_futopt_invalid_session_is_rejected(service):
    with pytest.raises(InvalidRequestError):
        service.futopt_intraday_candles("TXFG6", "1", "night")


def test_futopt_quote_with_session(service, fake_sdk):
    fake_sdk.responses["futopt.intraday.quote"] = {"symbol": "TXFG6", "lastPrice": 22000, "previousClose": 21900}

    quote = service.futopt_quote("TXFG6", "afterhours")

    assert fake_sdk.calls == [("futopt.intraday.quote", {"symbol": "TXFG6", "session": "afterhours"})]
    assert quote["previousClose"] == 21900.0
    assert quote["session"] == "afterhours"


def test_futopt_tickers(service, fake_sdk):
    fake_sdk.responses["futopt.intraday.tickers"] = {
        "data": [{"symbol": "TXFG6", "name": "臺股期貨", "endDate": "2026-07-15", "settlementDate": "2026-07-15"}]
    }

    body = service.futopt_tickers("FUTURE")

    assert fake_sdk.calls == [("futopt.intraday.tickers", {"type": "FUTURE"})]
    assert body["tickers"][0]["settlementDate"] == "2026-07-15"
    with pytest.raises(InvalidRequestError):
        service.futopt_tickers("SPOT")


# --------------------------------------------------------------------------- errors


def test_empty_data_is_not_found(service, fake_sdk):
    fake_sdk.responses["stock.intraday.candles"] = {"data": []}
    fake_sdk.responses["stock.intraday.quote"] = {}

    with pytest.raises(NotFoundError):
        service.stock_intraday_candles("2330", "1")
    with pytest.raises(NotFoundError):
        service.stock_quote("2330")


def test_upstream_404_is_not_found(service, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = FakeFugleError("Resource Not Found", status_code=404)

    with pytest.raises(NotFoundError):
        service.stock_quote("9999")


def test_payload_status_code_error_is_mapped(service, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = {"statusCode": 404, "message": "Not Found"}

    with pytest.raises(NotFoundError):
        service.stock_quote("9999")


def test_sdk_exception_is_upstream_error_without_secrets(service, fake_sdk, caplog):
    fake_sdk.responses["stock.intraday.quote"] = FakeFugleError(
        f"boom token={TOKEN} pw=test-password", status_code=500
    )

    with caplog.at_level(logging.WARNING, logger="fubon_sidecar"):
        with pytest.raises(UpstreamError) as excinfo:
            service.stock_quote("2330")

    assert excinfo.value.message == "Fubon market data request failed (HTTP 500)"
    assert TOKEN not in caplog.text and "test-password" not in caplog.text


def test_upstream_401_refreshes_realtime_token_once(service, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = sequence(
        FakeFugleError("Unauthorized", status_code=401), {"symbol": "2330", "lastPrice": 1}
    )

    quote = service.stock_quote("2330")

    assert quote["lastPrice"] == 1.0
    assert fake_sdk.realtime_inits == 2


def test_repeated_401_becomes_upstream_error(service, fake_sdk):
    fake_sdk.responses["stock.intraday.quote"] = FakeFugleError("Unauthorized", status_code=401)

    with pytest.raises(UpstreamError):
        service.stock_quote("2330")
    assert fake_sdk.realtime_inits == 2


def test_slow_upstream_times_out(make_service, fake_sdk):
    release = threading.Event()

    def slow(**_params):
        release.wait(5)
        return {"symbol": "2330"}

    service = make_service(settings={"request_timeout_sec": 0.2})
    fake_sdk.responses["stock.intraday.quote"] = slow
    started = time.monotonic()
    try:
        with pytest.raises(UpstreamError, match="timed out"):
            service.stock_quote("2330")
    finally:
        release.set()
    assert time.monotonic() - started < 2


# --------------------------------------------------------------------------- rate limit


def test_rate_limiter_spaces_calls():
    clock = FakeClock()
    limiter = RateLimiter(2.0, clock=clock, sleep=clock.sleep)

    limiter.acquire(10)
    limiter.acquire(10)
    limiter.acquire(10)

    assert clock.sleeps == [0.5, 0.5]


def test_rate_limiter_refuses_when_wait_exceeds_budget():
    clock = FakeClock()
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)

    limiter.acquire(0)
    with pytest.raises(RateLimitedError):
        limiter.acquire(0.5)


def test_service_applies_rate_limit(make_service, fake_sdk):
    clock = FakeClock()
    service = make_service(settings={"rate_limit_per_sec": 4.0}, clock=clock, sleep=clock.sleep)
    fake_sdk.responses["stock.intraday.quote"] = {"symbol": "2330", "lastPrice": 1}

    for _ in range(3):
        service.stock_quote("2330")

    assert clock.sleeps == [0.25, 0.25]


def test_service_is_constructible_with_default_factory():
    # The default factory imports fubon_neo lazily, so construction must not need the SDK.
    from conftest import make_settings

    service = FubonMarketData(make_settings())
    try:
        assert service.health()["logged_in"] is False
    finally:
        service.close()
