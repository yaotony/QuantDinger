"""Unit tests for the TWStock data source (Fubon sidecar client).

The sidecar HTTP layer is replaced by a fake session; no test reaches the
sidecar or Fubon.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

import pytest
import requests

from app.config import data_sources as data_source_config
from app.data_sources.factory import DataSourceFactory
from app.data_sources.tw_stock import (
    TIMEFRAME_MAP,
    FubonSidecarError,
    TWStockDataSource,
    normalize_tw_symbol,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _taipei_ts(text: str) -> int:
    fmt = "%Y-%m-%d %H:%M" if " " in text else "%Y-%m-%d"
    return int(datetime.strptime(text, fmt).replace(tzinfo=TAIPEI).timestamp())


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Routes sidecar paths to handlers and records every call."""

    def __init__(self, handlers: Dict[str, Callable[[Dict[str, Any]], FakeResponse]]) -> None:
        self.handlers = handlers
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def get(self, url: str, params=None, headers=None, timeout=None) -> FakeResponse:
        path = url.split("http://sidecar.test", 1)[-1]
        self.calls.append({"path": path, "params": dict(params or {}), "headers": dict(headers or {}),
                           "timeout": timeout})
        handler = self.handlers.get(path)
        if handler is None:
            return FakeResponse(404, {"code": "notFound"})
        return handler(dict(params or {}))


def _candles(rows: List[tuple]) -> Dict[str, Any]:
    return {
        "candles": [
            {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": v}
            for t, o, h, low, c, v in rows
        ]
    }


@pytest.fixture
def sidecar_config(monkeypatch):
    meta = data_source_config.MetaFubonSidecarConfig
    monkeypatch.setattr(meta, "BASE_URL", property(lambda cls: "http://sidecar.test"))
    monkeypatch.setattr(meta, "TOKEN", property(lambda cls: "test-token"))
    monkeypatch.setattr(meta, "TIMEOUT", property(lambda cls: 3.0))


def _source(handlers) -> tuple[TWStockDataSource, FakeSession]:
    src = TWStockDataSource()
    session = FakeSession(handlers)
    src._session = session  # type: ignore[assignment]
    return src, session


# --- symbols / factory ---------------------------------------------------------


def test_factory_recognizes_twstock_aliases():
    for alias in ("TWStock", "twstock", "TW", "TWSE", "TPEX", "TaiwanStock"):
        assert DataSourceFactory.normalize_market(alias) == "TWStock"
    assert "TWStock" in DataSourceFactory._CANONICAL_MARKETS
    assert isinstance(DataSourceFactory._create_source("TWStock"), TWStockDataSource)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("2330.TW", "2330"),
        ("0050.tw", "0050"),
        ("00631L.TW", "00631L"),
        ("6488.TWO", "6488"),
        (" 6488.two ", "6488"),
        ("IX0001", "IX0001"),
        ("ix0043", "IX0043"),
        ("TWStock:2330.TW", "2330"),
        ("2330", "2330"),
    ],
)
def test_normalize_symbol(raw, code):
    assert normalize_tw_symbol(raw) == code


@pytest.mark.parametrize("raw", ["", "AAPL", "../etc", "2330.HK", "23", "IX01", "2330&x=1.TW"])
def test_normalize_symbol_rejects_invalid(raw):
    assert normalize_tw_symbol(raw) == ""


def test_timeframe_map_covers_quantdinger_timeframes():
    assert set(TIMEFRAME_MAP) == {"1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"}
    assert TIMEFRAME_MAP["1D"] == ("D", False)
    assert TIMEFRAME_MAP["1W"] == ("W", False)
    assert TIMEFRAME_MAP["3m"] == ("1", True)
    assert TIMEFRAME_MAP["4H"] == ("60", True)


# --- K-lines ---------------------------------------------------------------------


@pytest.mark.parametrize(("symbol", "code"), [("2330.TW", "2330"), ("6488.TWO", "6488"), ("IX0001", "IX0001")])
def test_daily_kline_requests_period_candles(sidecar_config, symbol, code):
    day1, day2 = _taipei_ts("2025-10-02"), _taipei_ts("2025-10-03")
    src, session = _source({
        "/v1/stock/candles": lambda p: FakeResponse(200, _candles([
            (day2, 11, 12, 10.5, 11.5, 2000),
            (day1, 10, 11, 9.5, 10.5, 1000),
        ])),
    })

    rows = src.get_kline(
        symbol, "1D", limit=100,
        after_time=_taipei_ts("2025-10-01"), before_time=_taipei_ts("2025-10-31"),
    )

    assert [r["time"] for r in rows] == [day1, day2]
    assert rows[0] == {"time": day1, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1000.0}
    call = session.calls[0]
    assert call["path"] == "/v1/stock/candles"
    assert call["params"] == {"symbol": code, "timeframe": "D", "from": "2025-10-01", "to": "2025-10-30"}
    assert call["headers"] == {"X-Sidecar-Token": "test-token"}
    assert len(session.calls) == 1


def test_weekly_kline_uses_w_timeframe(sidecar_config):
    src, session = _source({"/v1/stock/candles": lambda p: FakeResponse(200, _candles([]))})

    src.get_kline("2330.TW", "1W", limit=20, before_time=_taipei_ts("2025-10-31"))

    assert session.calls[0]["params"]["timeframe"] == "W"


def test_daily_range_is_capped_to_one_year(sidecar_config):
    src, session = _source({"/v1/stock/candles": lambda p: FakeResponse(200, _candles([]))})

    src.get_kline(
        "2330.TW", "1D", limit=1000,
        after_time=_taipei_ts("2024-01-01"), before_time=_taipei_ts("2025-10-01"),
    )

    params = session.calls[0]["params"]
    assert params["to"] == "2025-09-30"
    assert params["from"] == "2024-09-30"


def test_minute_kline_omits_range_and_merges_intraday(sidecar_config):
    t0 = _taipei_ts("2025-10-30 09:00")
    src, session = _source({
        "/v1/stock/candles": lambda p: FakeResponse(200, _candles([
            (t0, 10, 10, 10, 10, 1),
            (t0 + 300, 11, 11, 11, 11, 1),
        ])),
        "/v1/stock/intraday/candles": lambda p: FakeResponse(200, _candles([
            (t0 + 300, 12, 12, 12, 12, 5),
            (t0 + 600, 13, 13, 13, 13, 6),
        ])),
    })

    rows = src.get_kline("2330.TW", "5m", limit=10)

    assert [r["time"] for r in rows] == [t0, t0 + 300, t0 + 600]
    assert rows[1]["close"] == 12.0  # intraday bar wins for the same timestamp
    assert session.calls[0]["params"] == {"symbol": "2330", "timeframe": "5"}
    assert session.calls[1]["path"] == "/v1/stock/intraday/candles"


def test_minute_kline_with_before_time_skips_intraday(sidecar_config):
    src, session = _source({"/v1/stock/candles": lambda p: FakeResponse(200, _candles([]))})

    src.get_kline("2330.TW", "1m", limit=10, before_time=_taipei_ts("2025-10-30 13:30"))

    assert [c["path"] for c in session.calls] == ["/v1/stock/candles"]


def test_3m_is_resampled_from_1m(sidecar_config):
    t0 = (_taipei_ts("2025-10-30 09:00") // 180) * 180
    src, _ = _source({
        "/v1/stock/candles": lambda p: FakeResponse(200, _candles([
            (t0, 10, 11, 9, 10.5, 1),
            (t0 + 60, 10.5, 12, 10, 11, 2),
            (t0 + 120, 11, 11.5, 8, 9, 3),
            (t0 + 180, 9, 9.5, 8.5, 9.2, 4),
        ])),
    })

    rows = src.get_kline("2330.TW", "3m", limit=10, before_time=t0 + 3600)

    assert rows == [
        {"time": t0, "open": 10.0, "high": 12.0, "low": 8.0, "close": 9.0, "volume": 6.0},
        {"time": t0 + 180, "open": 9.0, "high": 9.5, "low": 8.5, "close": 9.2, "volume": 4.0},
    ]


def test_4h_is_resampled_from_60m(sidecar_config):
    t0 = (_taipei_ts("2025-10-30 09:00") // 14400) * 14400
    src, session = _source({
        "/v1/stock/candles": lambda p: FakeResponse(200, _candles([
            (t0 + 3600, 10, 11, 9, 10, 1),
            (t0 + 7200, 10, 13, 10, 12, 1),
        ])),
    })

    rows = src.get_kline("IX0001", "4H", limit=10, before_time=t0 + 86400)

    assert session.calls[0]["params"]["timeframe"] == "60"
    assert rows == [{"time": t0, "open": 10.0, "high": 13.0, "low": 9.0, "close": 12.0, "volume": 2.0}]


def test_invalid_symbol_returns_empty_without_request(sidecar_config):
    src, session = _source({})
    assert src.get_kline("AAPL", "1D", limit=10) == []
    assert session.calls == []


def test_not_found_returns_empty(sidecar_config):
    src, _ = _source({"/v1/stock/candles": lambda p: FakeResponse(404, {"code": "notFound"})})
    assert src.get_kline("9999.TW", "1D", limit=10, before_time=_taipei_ts("2025-10-01")) == []


# --- sidecar failures -------------------------------------------------------------


def test_missing_token_is_a_clear_error(monkeypatch):
    meta = data_source_config.MetaFubonSidecarConfig
    monkeypatch.setattr(meta, "TOKEN", property(lambda cls: ""))
    src, session = _source({})

    with pytest.raises(FubonSidecarError, match="FUBON_SIDECAR_TOKEN"):
        src.get_kline("2330.TW", "1D", limit=10)
    assert session.calls == []


def _raising_session(exc: Exception) -> Callable[..., FakeResponse]:
    def _get(*_args, **_kwargs):
        raise exc
    return _get


def test_unreachable_sidecar_is_reported_by_factory(sidecar_config, monkeypatch):
    src, session = _source({})
    session.get = _raising_session(requests.ConnectionError("boom"))  # type: ignore[method-assign]
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", src)

    rows, failure = DataSourceFactory.get_kline_with_diagnostics(
        market="TWStock", symbol="2330.TW", timeframe="1D", limit=10,
    )

    assert rows == []
    assert failure is not None
    assert failure.code == "exchange_unavailable"
    assert "sidecar" in failure.technical_detail
    assert "test-token" not in failure.technical_detail


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (503, {"code": "notReady", "message": "not logged in"}, "not ready"),
        (401, {"code": "unauthorized"}, "FUBON_SIDECAR_TOKEN"),
        (429, {"code": "rateLimited"}, "rate limit"),
        (502, {"code": "upstreamError", "message": "sdk timeout"}, "HTTP 502"),
        (400, {"code": "invalidRequest", "message": "bad symbol"}, "400"),
    ],
)
def test_sidecar_error_statuses_raise(sidecar_config, status, body, match):
    src, _ = _source({"/v1/stock/candles": lambda p: FakeResponse(status, body)})
    with pytest.raises(FubonSidecarError, match=match):
        src.get_kline("2330.TW", "1D", limit=10, before_time=_taipei_ts("2025-10-01"))


def test_timeout_is_classified_as_unavailable(sidecar_config, monkeypatch):
    src, session = _source({})
    session.get = _raising_session(requests.Timeout("slow"))  # type: ignore[method-assign]
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", src)

    _, failure = DataSourceFactory.get_kline_with_diagnostics(
        market="TWStock", symbol="IX0001", timeframe="1D", limit=10,
    )

    assert failure is not None and failure.code == "exchange_unavailable"


# --- ticker ---------------------------------------------------------------------


def test_get_ticker_matches_hk_shape(sidecar_config):
    src, session = _source({
        "/v1/stock/quote": lambda p: FakeResponse(200, {
            "symbol": "2330", "name": "台積電", "lastPrice": 1000.0, "change": 10.0,
            "changePercent": 1.01, "open": 995.0, "high": 1005.0, "low": 990.0,
            "previousClose": 990.0, "referencePrice": 990.0, "volume": 12345, "time": 1,
        }),
    })

    ticker = src.get_ticker("2330.TW")

    assert ticker == {
        "last": 1000.0, "change": 10.0, "changePercent": 1.01, "high": 1005.0,
        "low": 990.0, "open": 995.0, "previousClose": 990.0, "name": "台積電",
        "symbol": "2330.TW",
    }
    assert session.calls[0]["params"] == {"symbol": "2330"}


def test_get_ticker_derives_change_when_missing(sidecar_config):
    src, _ = _source({
        "/v1/stock/quote": lambda p: FakeResponse(200, {"lastPrice": 110.0, "referencePrice": 100.0}),
    })
    ticker = src.get_ticker("6488.TWO")
    assert ticker["previousClose"] == 100.0
    assert ticker["change"] == 10.0
    assert ticker["changePercent"] == 10.0


def test_factory_ticker_failure_returns_zero(sidecar_config, monkeypatch):
    src, session = _source({})
    session.get = _raising_session(requests.ConnectionError("down"))  # type: ignore[method-assign]
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", src)

    assert DataSourceFactory.get_ticker("TWStock", "2330.TW") == {"last": 0, "symbol": "2330.TW"}


# --- earliest bar -----------------------------------------------------------------


def _expected_one_year_back() -> int:
    day = datetime.now(tz=TAIPEI).date() - timedelta(days=365)
    return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI).timestamp())


@pytest.mark.parametrize("timeframe", ["1D", "1W", "1d"])
def test_earliest_daily_is_one_year_back(sidecar_config, timeframe):
    src, session = _source({})
    assert src.get_earliest_kline_time("2330.TW", timeframe) == _expected_one_year_back()
    assert session.calls == []


def test_earliest_minute_is_first_of_latest_five_days(sidecar_config):
    first = _taipei_ts("2025-10-24 09:00")
    src, session = _source({
        "/v1/stock/candles": lambda p: FakeResponse(200, _candles([
            (first + 60, 1, 1, 1, 1, 1),
            (first, 1, 1, 1, 1, 1),
        ])),
    })

    assert src.get_earliest_kline_time("6488.TWO", "15m") == first
    assert session.calls[0]["params"] == {"symbol": "6488", "timeframe": "15"}


def test_earliest_unknown_when_sidecar_fails_or_symbol_invalid(sidecar_config):
    src, session = _source({})
    session.get = _raising_session(requests.ConnectionError("down"))  # type: ignore[method-assign]
    assert src.get_earliest_kline_time("2330.TW", "1m") is None
    assert src.get_earliest_kline_time("AAPL", "1D") is None
    assert src.get_earliest_kline_time("2330.TW", "2h") is None
