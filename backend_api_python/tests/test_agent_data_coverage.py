"""Agent data-coverage query (#40).

Data sources are mocked throughout; no test reaches an exchange.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.data_sources.base import BaseDataSource
from app.data_sources.crypto import CryptoDataSource
from app.data_sources.factory import DataSourceFactory
from app.services import data_coverage
from app.services.backtest_limits import BacktestRangeLimitError
from app.services.data_coverage import DataCoverageRequestError, describe_data_coverage
from app.services.strategy_v2 import service as strategy_service
from app.services.strategy_v2.models import (
    InstrumentSpec, StrategyManifest, SubscriptionSpec, UniverseSpec,
)
from app.services.strategy_v2.service import precheck_backtest_range
from app.utils import agent_auth


def _unknown(**_kwargs):
    return None


def _manifest(market: str, symbol: str, frequency: str, warmup: int, **venue) -> StrategyManifest:
    instrument = InstrumentSpec(market=market, symbol=symbol, **venue)
    return StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cta",
        universe=UniverseSpec(kind="static", instruments=(instrument,)),
        subscriptions=(SubscriptionSpec(instruments=(instrument,), frequency=frequency),),
        schedules=(),
        handlers=("handle_data",),
        warmup_bars=warmup,
    )


def _precheck(manifest: StrategyManifest, start: datetime, end: datetime):
    try:
        precheck_backtest_range(manifest, start_date=start, end_date=end)
    except BacktestRangeLimitError as exc:
        return exc.details
    return None


def _day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d")


CASES = [
    ("Crypto", "BTC/USDT", "15m", 0, "2023-01-01", "2024-12-31"),
    ("Crypto", "BTC/USDT", "15m", 40_000, "2024-06-01", "2024-06-30"),
    ("Crypto", "BTC/USDT", "15m", 96, "2024-01-01", "2024-06-30"),
    ("Crypto", "ETH/USDT", "1h", 200, "2021-01-01", "2024-06-30"),
    ("Crypto", "ETH/USDT", "1d", 300, "2022-01-01", "2024-12-31"),
    ("USStock", "AAPL", "15m", 50, "2024-01-01", "2024-03-15"),
    ("USStock", "AAPL", "1h", 400, "2023-01-01", "2024-06-30"),
    ("Forex", "EURUSD", "1m", 0, "2024-06-01", "2024-06-05"),
]


@pytest.mark.parametrize(("market", "symbol", "frequency", "warmup", "start", "end"), CASES)
def test_coverage_range_verdict_matches_backtest_precheck(market, symbol, frequency, warmup, start, end):
    manifest = _manifest(market, symbol, frequency, warmup)
    expected = _precheck(manifest, _day(start), _day(end))

    coverage = describe_data_coverage(
        market=market, symbol=symbol, timeframe=frequency,
        start_date=_day(start), end_date=_day(end), warmup_bars=warmup,
        earliest_lookup=_unknown,
    )
    evaluation = coverage["evaluation"]

    assert evaluation["range_check"]["violation"] == expected
    assert evaluation["range_check"]["ok"] is (expected is None)
    # With an unknown first bar only the shared range rule decides.
    assert evaluation["feasible"] is (expected is None)
    if expected is not None:
        assert coverage["limits"]["max_days"] == expected["max_days"]
        assert coverage["limits"]["warmup_days"] == expected["warmup_days"]


@pytest.mark.parametrize(("market", "symbol", "frequency", "warmup", "start", "end"), CASES)
def test_recommended_range_is_accepted_by_backtest_precheck(market, symbol, frequency, warmup, start, end):
    coverage = describe_data_coverage(
        market=market, symbol=symbol, timeframe=frequency,
        start_date=_day(start), end_date=_day(end), warmup_bars=warmup,
        earliest_lookup=_unknown,
    )
    evaluation = coverage["evaluation"]
    if evaluation["feasible"]:
        assert evaluation["recommended_start"] is None
        return
    violation = evaluation["range_check"]["violation"]
    if not violation["recommendation_available"]:
        assert evaluation["recommended_start"] is None
        assert evaluation["recommended_end"] is None
        assert coverage["limits"]["warmup_fits"] is False
        return
    assert evaluation["recommended_end"] == end
    manifest = _manifest(market, symbol, frequency, warmup)
    assert _precheck(
        manifest, _day(evaluation["recommended_start"]), _day(evaluation["recommended_end"]),
    ) is None


def test_limits_without_dates_and_unknown_earliest_bar():
    coverage = describe_data_coverage(
        market="Crypto", symbol="BTC/USDT", timeframe="15m", warmup_bars=96,
        earliest_lookup=_unknown,
    )

    assert coverage["timeframe"] == "15m"
    assert coverage["earliest_bar"] == {"status": "unknown", "time": None, "timestamp": None}
    assert coverage["evaluation"] is None
    limits = coverage["limits"]
    assert limits["validation_market"] == "Crypto"
    assert limits["max_days"] == 365
    assert limits["warmup_days"] == 2
    assert limits["max_selected_days"] == 363
    assert limits["warmup_fits"] is True


def test_unknown_earliest_bar_leaves_history_check_unknown():
    coverage = describe_data_coverage(
        market="Crypto", symbol="BTC/USDT", timeframe="1d",
        start_date=_day("2010-01-01"), end_date=_day("2010-06-30"),
        earliest_lookup=_unknown,
    )

    history = coverage["evaluation"]["history_check"]
    assert history["status"] == "unknown"
    assert history["earliest_start"] is None
    assert coverage["evaluation"]["feasible"] is True


def test_known_earliest_bar_flags_and_fixes_missing_history():
    first_bar = int(datetime(2024, 3, 10, 8, 0, tzinfo=timezone.utc).timestamp())
    seen = {}

    def lookup(**kwargs):
        seen.update(kwargs)
        return first_bar

    coverage = describe_data_coverage(
        market="Crypto", symbol="BTC/USDT", timeframe="1h",
        exchange_id="Binance", market_type="spot",
        start_date=_day("2024-03-01"), end_date=_day("2024-06-30"), warmup_bars=48,
        earliest_lookup=lookup,
    )

    assert seen["timeframe"] == "1H"
    assert seen["exchange_id"] == "binance"
    assert coverage["earliest_bar"] == {
        "status": "known", "time": "2024-03-10T08:00:00Z", "timestamp": first_bar,
    }
    evaluation = coverage["evaluation"]
    assert evaluation["range_check"]["ok"] is True
    assert evaluation["history_check"]["status"] == "insufficient"
    # First full day 2024-03-11 plus 3 warmup days (48 x 1h x 1.5).
    assert coverage["limits"]["warmup_days"] == 3
    assert evaluation["history_check"]["earliest_start"] == "2024-03-14"
    assert evaluation["feasible"] is False
    assert evaluation["recommended_start"] == "2024-03-14"
    assert evaluation["recommended_end"] == "2024-06-30"


def test_known_earliest_bar_before_fetch_window_is_ok():
    coverage = describe_data_coverage(
        market="Crypto", symbol="BTC/USDT", timeframe="1d",
        start_date=_day("2024-01-01"), end_date=_day("2024-06-30"), warmup_bars=20,
        earliest_lookup=lambda **_: int(datetime(2017, 8, 17, tzinfo=timezone.utc).timestamp()),
    )

    evaluation = coverage["evaluation"]
    assert evaluation["history_check"]["status"] == "ok"
    assert evaluation["history_check"]["earliest_start"] == "2017-09-24"
    assert evaluation["feasible"] is True


def test_coverage_uses_catalog_underlying_market_like_backtest(monkeypatch):
    monkeypatch.setattr(strategy_service, "get_catalog_product", lambda **_kw: {
        "instrument_id": "AAPLX",
        "product_type": "direct_equity",
        "api_family": "spot",
        "underlying_market": "USStock",
        "underlying_symbol": "AAPL",
    })
    coverage = describe_data_coverage(
        market="Crypto", symbol="AAPLX/USDT", timeframe="1d", exchange_id="bitget",
        start_date=_day("2020-01-01"), end_date=_day("2024-12-31"),
        earliest_lookup=_unknown,
    )
    manifest = _manifest("Crypto", "AAPLX/USDT", "1d", 0, exchange_id="bitget")

    assert coverage["limits"]["validation_market"] == "USStock"
    assert coverage["limits"]["max_days"] == 3650
    assert coverage["evaluation"]["feasible"] is True
    assert _precheck(manifest, _day("2020-01-01"), _day("2024-12-31")) is None


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"timeframe": "2h"}, "timeframe must be one of"),
    ({"start_date": datetime(2024, 1, 1)}, "provided together"),
    ({"start_date": datetime(2024, 2, 1), "end_date": datetime(2024, 1, 1)}, "earlier"),
    ({"warmup_bars": -1}, "non-negative"),
])
def test_invalid_coverage_input(kwargs, message):
    params = {"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1d", **kwargs}
    with pytest.raises(DataCoverageRequestError, match=message):
        describe_data_coverage(earliest_lookup=_unknown, **params)


# --- data source lookup -------------------------------------------------------


class _FakeExchange:
    timeframes = {"1h": "1h", "1d": "1d"}

    def __init__(self, exchange_id: str, rows):
        self.id = exchange_id
        self.rows = rows
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=300):
        self.calls.append((symbol, timeframe, since, limit))
        return self.rows


def _crypto_source(exchange: _FakeExchange) -> CryptoDataSource:
    source = object.__new__(CryptoDataSource)
    source.exchange = exchange
    source._scoped_market_type = "spot"
    source._markets_loaded = False
    source._markets_cache = None
    return source


def test_binance_reports_first_candle_from_epoch():
    exchange = _FakeExchange("binance", [[1_502_928_000_000, 1, 1, 1, 1, 1]])
    source = _crypto_source(exchange)
    source._normalize_symbol_for_exchange = lambda symbol: symbol

    assert source.get_earliest_kline_time("BTC/USDT", "1D") == 1_502_928_000
    assert exchange.calls == [("BTC/USDT", "1d", 0, 1)]


@pytest.mark.parametrize(("exchange_id", "timeframe", "rows"), [
    ("okx", "1D", [[1_502_928_000_000, 1, 1, 1, 1, 1]]),
    ("binance", "4H", [[1_502_928_000_000, 1, 1, 1, 1, 1]]),
    ("binance", "1D", []),
])
def test_crypto_first_candle_is_unknown_when_venue_cannot_report_it(exchange_id, timeframe, rows):
    exchange = _FakeExchange(exchange_id, rows)
    source = _crypto_source(exchange)
    source._normalize_symbol_for_exchange = lambda symbol: symbol

    assert source.get_earliest_kline_time("BTC/USDT", timeframe) is None
    if exchange_id == "okx" or timeframe == "4H":
        assert exchange.calls == []


def test_base_data_source_reports_unknown():
    class Plain(BaseDataSource):
        def get_kline(self, *args, **kwargs):
            return []

    assert Plain().get_earliest_kline_time("AAPL", "1D") is None


def test_factory_turns_provider_failures_into_unknown(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        DataSourceFactory, "_resolve_source",
        classmethod(lambda cls, market, **kw: SimpleNamespace(get_earliest_kline_time=broken)),
    )
    assert DataSourceFactory.get_earliest_kline_time(
        market="Crypto", symbol="BTC/USDT", timeframe="1D",
    ) is None

    monkeypatch.setattr(
        DataSourceFactory, "_resolve_source",
        classmethod(lambda cls, market, **kw: SimpleNamespace()),
    )
    assert DataSourceFactory.get_earliest_kline_time(
        market="USStock", symbol="AAPL", timeframe="1D",
    ) is None

    monkeypatch.setattr(
        DataSourceFactory, "_resolve_source",
        classmethod(lambda cls, market, **kw: SimpleNamespace(
            get_earliest_kline_time=lambda symbol, timeframe: 1_502_928_000)),
    )
    assert DataSourceFactory.get_earliest_kline_time(
        market="Crypto", symbol="BTC/USDT", timeframe="1D", exchange_id="binance",
    ) == 1_502_928_000


# --- gateway route ------------------------------------------------------------


@pytest.fixture
def read_token(monkeypatch):
    token = {"id": 7, "user_id": 5, "scopes": "R", "markets": "Crypto,USStock",
             "instruments": "*", "status": "active", "rate_limit_per_min": 100,
             "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(DataSourceFactory, "get_earliest_kline_time", staticmethod(_unknown))
    yield token
    agent_auth._rate_state.clear()


def _get(client, **params):
    return client.get(
        "/api/agent/v1/data-coverage",
        query_string=params,
        headers={"Authorization": "Bearer qd_agent_COVERAGETEST1"},
    )


def test_route_returns_coverage_with_read_scope(client, read_token):
    response = _get(
        client, market="Crypto", symbol="BTC/USDT", timeframe="15m",
        start_date="2023-01-01", end_date="2024-12-31", warmup_bars="200",
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["earliest_bar"]["status"] == "unknown"
    assert data["limits"]["max_days"] == 365
    evaluation = data["evaluation"]
    expected = _precheck(
        _manifest("Crypto", "BTC/USDT", "15m", 200), _day("2023-01-01"), _day("2024-12-31"),
    )
    assert evaluation["feasible"] is False
    assert evaluation["range_check"]["violation"] == expected
    assert evaluation["recommended_end"] == "2024-12-31"


def test_route_without_dates_returns_limits_only(client, read_token):
    response = _get(client, market="crypto", symbol="BTC/USDT", timeframe="1D")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["market"] == "Crypto"
    assert data["timeframe"] == "1d"
    assert data["evaluation"] is None


@pytest.mark.parametrize(("params", "status"), [
    ({"symbol": "BTC/USDT", "timeframe": "1d"}, 400),
    ({"market": "Crypto", "symbol": "BTC/USDT"}, 400),
    ({"market": "Mars", "symbol": "BTC/USDT", "timeframe": "1d"}, 400),
    ({"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "2h"}, 400),
    ({"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1d", "start_date": "2024-01-01"}, 400),
    ({"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1d",
      "start_date": "2024-13-01", "end_date": "2024-12-31"}, 400),
    ({"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1d", "warmup_bars": "x"}, 400),
    ({"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1d", "warmup_bars": "-5"}, 400),
    ({"market": "Forex", "symbol": "EURUSD", "timeframe": "1d"}, 403),
])
def test_route_rejects_invalid_or_disallowed_input(client, read_token, params, status):
    assert _get(client, **params).status_code == status


def test_route_respects_instrument_allowlist(client, read_token):
    read_token["instruments"] = "ETH/USDT"
    response = _get(client, market="Crypto", symbol="BTC/USDT", timeframe="1d")
    assert response.status_code == 403


def test_route_module_uses_shared_coverage_service():
    assert data_coverage.backtest_range_violation is strategy_service.backtest_range_violation
