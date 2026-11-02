"""TWStock market registration, routing, limits and live-trading guard (#54).

The Fubon sidecar is never contacted: data sources are faked throughout.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.config import data_sources as data_source_config
from app.data_sources.base import BaseDataSource
from app.data_sources.errors import MarketDataUnavailableError
from app.data_sources.factory import DataSourceFactory
from app.data_sources.tw_stock import TWStockDataSource
from app.markets.registry import MARKET_MODULES, list_market_modules
from app.services.backtest_limits import (
    backtest_range_limit_summary,
    backtest_range_policy,
)
from app.services.broker_market_policy import (
    BROKER_MARKETS,
    LIVE_MARKET_CATEGORIES,
    validate_strategy_config,
)
from app.services.data_coverage import describe_data_coverage
from app.services.market import watchlist
from app.services.market_schedule import (
    CALENDAR_BY_MARKET,
    equity_bar_session_date,
    equity_daily_bar_cutoff,
)
from app.services.strategy_v2 import market_data
from app.services.strategy_v2.instruments import infer_market, parse_instrument
from app.services.strategy_v2.service import backtest_range_candidates, backtest_warmup_days
from app.utils import agent_auth
from app.utils.market_visibility import is_market_visible

TAIPEI = ZoneInfo("Asia/Taipei")


# --- instruments ------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["2330.TW", "0050.TW", "6488.TWO", "IX0001", "IX0043", "2330.tw"])
def test_infer_market_recognizes_taiwan_symbols(symbol):
    assert infer_market(symbol) == "TWStock"


@pytest.mark.parametrize(
    ("symbol", "market"),
    [("AAPL", "USStock"), ("BRK.B", "USStock"), ("TW", "USStock"), ("IXN", "USStock"),
     ("600519", "CNStock"), ("00700.HK", "HKStock"), ("BTC/USDT", "Crypto")],
)
def test_infer_market_keeps_existing_markets(symbol, market):
    assert infer_market(symbol) == market


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [
        ("TWStock:2330.TW", "2330.TW"),
        ("twstock:6488.two", "6488.TWO"),
        ("TWStock:IX0001", "IX0001"),
        ("2330.TW", "2330.TW"),
        ("6488.TPEX", "6488.TWO"),
        ("2330.TWSE", "2330.TW"),
        ("ix0001", "IX0001"),
    ],
)
def test_parse_instrument_normalizes_taiwan_symbols(raw, symbol):
    spec = parse_instrument(raw)
    assert spec.market == "TWStock"
    assert spec.symbol == symbol
    assert spec.exchange_id == "" and spec.market_type == ""


# --- registration / visibility ----------------------------------------------------


def test_market_module_registered_without_live():
    module = MARKET_MODULES["TWStock"]
    assert module.features == ["research", "backtest", "paper"]
    assert "live" not in module.features
    assert module.data_requirements[0].setting_keys == ["FUBON_SIDECAR_TOKEN"]


def test_market_module_status_follows_token_and_enabled_markets():
    blocked = {m["key"]: m for m in list_market_modules({"ENABLED_MARKETS": ""})}
    assert blocked["TWStock"]["enabled"] is True
    assert blocked["TWStock"]["status"] == "blocked"
    assert blocked["TWStock"]["live_brokers"] == []

    ready = {m["key"]: m for m in list_market_modules({"FUBON_SIDECAR_TOKEN": "test-token"})}
    assert ready["TWStock"]["status"] == "ready"

    hidden = {m["key"]: m for m in list_market_modules({"ENABLED_MARKETS": "Crypto,USStock"})}
    assert hidden["TWStock"]["enabled"] is False


def test_enabled_markets_controls_visibility(monkeypatch):
    monkeypatch.delenv("ENABLED_MARKETS", raising=False)
    assert is_market_visible("TWStock") is True
    monkeypatch.setenv("ENABLED_MARKETS", "Crypto,TWStock")
    assert is_market_visible("TWStock") is True
    monkeypatch.setenv("ENABLED_MARKETS", "Crypto,USStock")
    assert is_market_visible("TWStock") is False


def test_market_types_route_lists_twstock(client, monkeypatch):
    monkeypatch.delenv("ENABLED_MARKETS", raising=False)
    values = [item["value"] for item in client.get("/api/market/types").get_json()["data"]]
    assert "TWStock" in values
    assert values.index("HKStock") < values.index("TWStock") < values.index("Crypto")

    monkeypatch.setenv("ENABLED_MARKETS", "USStock")
    values = [item["value"] for item in client.get("/api/market/types").get_json()["data"]]
    assert "TWStock" not in values


def test_watchlist_validation():
    assert watchlist.validate_watchlist_pair("TWStock", "2330.TW") is None
    assert watchlist.validate_watchlist_pair("TWStock", "6488.TWO") is None
    assert watchlist.validate_watchlist_pair("TWStock", "IX0001") is None
    assert "TWStock" in watchlist.validate_watchlist_pair("USStock", "2330.TW")
    assert watchlist.validate_watchlist_pair("TWStock", "AAPL") is not None


# --- live trading is rejected ---------------------------------------------------


def test_twstock_is_not_a_live_market():
    assert "TWStock" not in LIVE_MARKET_CATEGORIES
    assert all("TWStock" not in markets for markets in BROKER_MARKETS.values())


@pytest.mark.parametrize("exchange_id", ["", "ibkr", "tiger", "binance"])
def test_live_strategy_config_for_twstock_is_rejected(exchange_id):
    with pytest.raises(ValueError, match="TWStock.*not supported for live trading"):
        validate_strategy_config(
            exchange_id=exchange_id,
            market_category="TWStock",
            market_type="spot",
            trade_direction="long",
            require_exchange=bool(exchange_id),
        )


# --- schedule / limits ------------------------------------------------------------


def test_trading_calendar_and_daily_cutoff_use_taipei():
    assert CALENDAR_BY_MARKET["TWStock"] == "XTAI"
    # 2025-10-30 (Thu) 14:00 Taipei: the session closed at 13:30 (+15 min delay).
    now = datetime(2025, 10, 30, 6, 0, tzinfo=timezone.utc)
    cutoff = equity_daily_bar_cutoff("TWStock", now)
    # Daily bars are labelled at Taipei midnight (2025-10-29 16:00 UTC).
    today_bar = pd.Timestamp("2025-10-29 16:00")
    assert today_bar <= cutoff < today_bar + pd.Timedelta(days=1)

    # Before the close the latest completed session is the previous day.
    before_close = equity_daily_bar_cutoff("TWStock", datetime(2025, 10, 30, 3, 0, tzinfo=timezone.utc))
    assert before_close < today_bar
    assert str(equity_bar_session_date(today_bar, "TWStock")) == "2025-10-30"


def test_backtest_range_policy():
    assert backtest_range_policy("TWStock", "1D").max_days == 365
    assert backtest_range_policy("TWStock", "1W").max_days == 365
    for tf in ("1m", "3m", "5m", "15m", "30m", "1H", "4H"):
        assert backtest_range_policy("TWStock", tf).max_days == 5
    assert backtest_range_policy("tw", "1d").max_days == 365
    summary = backtest_range_limit_summary(["TWStock"])
    assert summary["markets"]["TWStock"]["1D"]["max_days"] == 365


def test_intraday_warmup_treats_twstock_as_stock_market():
    candidates = backtest_range_candidates([parse_instrument("TWStock:2330.TW")])
    crypto = backtest_range_candidates([parse_instrument("Crypto:BTC/USDT")])
    assert backtest_warmup_days("5m", 60, candidates) >= 7
    assert backtest_warmup_days("5m", 60, candidates) > backtest_warmup_days("5m", 60, crypto)


def test_data_coverage_matches_backtest_rules():
    coverage = describe_data_coverage(
        market="TWStock", symbol="2330.TW", timeframe="1D",
        start_date=datetime(2025, 1, 1), end_date=datetime(2026, 6, 30),
        earliest_lookup=lambda **_: None,
    )
    assert coverage["limits"]["validation_market"] == "TWStock"
    assert coverage["limits"]["max_days"] == 365
    evaluation = coverage["evaluation"]
    assert evaluation["feasible"] is False
    assert evaluation["range_check"]["violation"]["max_days"] == 365

    minute = describe_data_coverage(market="TWStock", symbol="IX0001", timeframe="5m",
                                    earliest_lookup=lambda **_: None)
    assert minute["limits"]["max_days"] == 5


def test_data_coverage_uses_source_earliest(monkeypatch):
    src = TWStockDataSource()
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", src)
    coverage = describe_data_coverage(market="TWStock", symbol="2330.TW", timeframe="1D")
    day = datetime.now(tz=TAIPEI).date() - timedelta(days=365)
    expected = int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI).timestamp())
    assert coverage["earliest_bar"] == {
        "status": "known",
        "time": datetime.fromtimestamp(expected, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "timestamp": expected,
    }


# --- backtest data loading --------------------------------------------------------


class _FakeTWSource(BaseDataSource):
    name = "fake-tw"

    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls = []

    def get_kline(self, symbol, timeframe, limit, before_time=None, after_time=None):
        self.calls.append((symbol, timeframe, before_time, after_time))
        if self.error:
            raise self.error
        return [dict(row) for row in self.rows]


@pytest.fixture
def _no_frame_cache(monkeypatch):
    market_data.clear_shared_strategy_frame_cache()
    monkeypatch.setattr(market_data._cache, "get", lambda _key: None)
    monkeypatch.setattr(market_data._cache, "put", lambda *_args: None)
    yield
    market_data.clear_shared_strategy_frame_cache()


def _daily_rows():
    out = []
    for day in ("2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06"):
        ts = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TAIPEI).timestamp())
        out.append({"time": ts, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10})
    return out


@pytest.mark.parametrize("symbol", ["2330.TW", "6488.TWO", "IX0001"])
def test_backtest_loads_twstock_daily_frame(monkeypatch, _no_frame_cache, symbol):
    source = _FakeTWSource(_daily_rows())
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", source)

    frame = market_data.load_strategy_frame(
        "TWStock", symbol, "1d", datetime(2025, 9, 30), datetime(2025, 10, 7),
    )

    assert len(frame) == 4
    assert source.calls[0][0] == symbol
    assert source.calls[0][1] == "1D"


def test_backtest_reports_sidecar_failure(monkeypatch, _no_frame_cache):
    from app.data_sources.tw_stock import FubonSidecarError

    source = _FakeTWSource(error=FubonSidecarError(
        "Fubon market data sidecar at http://fubon-marketdata:8010 is unreachable "
        "(connection refused or network error: ConnectionError)."
    ))
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", source)

    with pytest.raises(MarketDataUnavailableError) as excinfo:
        market_data.load_strategy_frame(
            "TWStock", "2330.TW", "1d", datetime(2025, 9, 30), datetime(2025, 10, 7),
        )
    assert excinfo.value.failure.code == "exchange_unavailable"
    assert "sidecar" in excinfo.value.failure.technical_detail


# --- agent gateway ----------------------------------------------------------------


@pytest.fixture
def tw_token(monkeypatch):
    token = {"id": 8, "user_id": 5, "scopes": "R", "markets": "TWStock,USStock",
             "instruments": "*", "status": "active", "rate_limit_per_min": 100,
             "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.delenv("ENABLED_MARKETS", raising=False)
    yield token
    agent_auth._rate_state.clear()


_HEADERS = {"Authorization": "Bearer qd_agent_TWSTOCKTEST1"}


def test_agent_markets_lists_twstock(client, tw_token):
    data = client.get("/api/agent/v1/markets", headers=_HEADERS).get_json()["data"]
    assert {"value": "TWStock", "label": "Taiwan Stocks"} in data


@pytest.mark.parametrize("symbol", ["2330.TW", "6488.TWO", "IX0001"])
def test_agent_klines_for_twstock(client, tw_token, monkeypatch, symbol):
    source = _FakeTWSource(_daily_rows())
    monkeypatch.setitem(DataSourceFactory._sources, "TWStock", source)

    response = client.get(
        "/api/agent/v1/klines",
        query_string={"market": "TWStock", "symbol": symbol, "timeframe": "1D",
                      "limit": 10, "before_time": 1_900_000_000},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["count"] == 4
    assert source.calls[0][0] == symbol


def test_agent_data_coverage_for_twstock(client, tw_token, monkeypatch):
    monkeypatch.setattr(
        DataSourceFactory, "_resolve_source",
        classmethod(lambda cls, market, **kw: SimpleNamespace(
            get_earliest_kline_time=lambda symbol, timeframe: 1_750_000_000)),
    )
    response = client.get(
        "/api/agent/v1/data-coverage",
        query_string={"market": "TWStock", "symbol": "2330.TW", "timeframe": "1D"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["market"] == "TWStock"
    assert data["earliest_bar"]["timestamp"] == 1_750_000_000
    assert data["limits"]["max_days"] == 365


def test_sidecar_config_defaults(monkeypatch):
    monkeypatch.setattr(data_source_config, "_addon", lambda *_: None)
    monkeypatch.delenv("FUBON_SIDECAR_URL", raising=False)
    monkeypatch.delenv("FUBON_SIDECAR_TOKEN", raising=False)
    monkeypatch.setenv("FUBON_SIDECAR_TIMEOUT", "bad")
    cfg = data_source_config.FubonSidecarConfig
    assert cfg.BASE_URL == "http://fubon-marketdata:8010"
    assert cfg.TOKEN == ""
    assert cfg.CONFIGURED is False
    assert cfg.TIMEOUT == 15.0
    monkeypatch.setenv("FUBON_SIDECAR_URL", "http://sidecar.test/")
    assert cfg.BASE_URL == "http://sidecar.test"
