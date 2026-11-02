"""TWFutures market registration, schedule, contract rules, catalog and backtests (#57).

The Fubon sidecar and PostgreSQL are never contacted: data sources are faked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data_sources.factory import DataSourceFactory
from app.data_sources.tw_futures import TWFuturesDataSource
from app.events.bar_clock import supports_bar_close_events
from app.markets.registry import MARKET_MODULES, MARKET_ORDER, list_market_modules
from app.services.backtest_limits import backtest_range_limit_summary, backtest_range_policy
from app.services.broker_market_policy import (
    BROKER_MARKETS,
    LIVE_MARKET_CATEGORIES,
    validate_strategy_config,
)
from app.services.data_coverage import describe_data_coverage
from app.services.instrument_rules import default_rules_for_symbol, tw_futures_rules
from app.services.market import watchlist
from app.services.market_schedule import (
    CALENDAR_BY_MARKET,
    equity_bar_session_date,
    equity_daily_bar_cutoff,
    is_equity_bar_close,
    latest_completed_session,
)
from app.services.strategy_v2 import StrategyV2BacktestService
from app.services.strategy_v2.instruments import parse_instrument
from app.services.strategy_v2.service import backtest_range_candidates, backtest_warmup_days
from app.services.symbol_master_sync import FETCHERS, fetch_tw_futures_symbols
from app.utils.market_visibility import is_market_visible

TAIPEI = ZoneInfo("Asia/Taipei")


def _tp(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TAIPEI)


# --- registration -----------------------------------------------------------------


@pytest.mark.parametrize("alias", ["TWFutures", "twfutures", "TAIFEX", "taifex", "TWF", "tw_futures"])
def test_market_aliases(alias):
    assert DataSourceFactory.normalize_market(alias) == "TWFutures"


def test_factory_creates_the_futures_source():
    assert "TWFutures" in DataSourceFactory._CANONICAL_MARKETS
    assert isinstance(DataSourceFactory._create_source("TWFutures"), TWFuturesDataSource)


@pytest.mark.parametrize("raw, symbol", [("TWFutures:TXF", "TXF"), ("taifex:txfh6", "TXFH6"), ("TWF:MXF", "MXF")])
def test_parse_instrument(raw, symbol):
    spec = parse_instrument(raw)
    assert (spec.market, spec.symbol, spec.key) == ("TWFutures", symbol, f"TWFutures:{symbol}")


def test_market_module_registered_without_live():
    module = MARKET_MODULES["TWFutures"]
    assert module.features == ["research", "backtest", "paper"]
    assert module.base_currency == "TWD"
    assert module.data_requirements[0].setting_keys == ["FUBON_SIDECAR_TOKEN"]
    assert "TWFutures" in MARKET_ORDER
    modules = {m["key"]: m for m in list_market_modules({"FUBON_SIDECAR_TOKEN": "test-token"})}
    assert modules["TWFutures"]["status"] == "ready"
    assert modules["TWFutures"]["live_brokers"] == []


def test_visibility_follows_enabled_markets(monkeypatch):
    monkeypatch.delenv("ENABLED_MARKETS", raising=False)
    assert is_market_visible("TWFutures") is True
    monkeypatch.setenv("ENABLED_MARKETS", "Crypto,TWStock")
    assert is_market_visible("TWFutures") is False


def test_market_types_route_lists_twfutures(client, monkeypatch):
    monkeypatch.delenv("ENABLED_MARKETS", raising=False)
    values = [item["value"] for item in client.get("/api/market/types").get_json()["data"]]
    assert values.index("TWStock") < values.index("TWFutures") < values.index("Crypto")


def test_agent_market_list_contains_twfutures():
    from app.routes.agent_v1.markets import _MARKETS

    assert {"value": "TWFutures", "label": "Taiwan Futures"} in _MARKETS


def test_watchlist_validation():
    assert watchlist.validate_watchlist_pair("TWFutures", "TXF") is None
    assert watchlist.validate_watchlist_pair("TWFutures", "TXFH6") is None
    assert watchlist.validate_watchlist_pair("TWFutures", "TXFM6") is not None
    assert watchlist.validate_watchlist_pair("TWFutures", "2330.TW") is not None


# --- live trading is rejected -----------------------------------------------------


def test_twfutures_is_not_a_live_market():
    assert "TWFutures" not in LIVE_MARKET_CATEGORIES
    assert all("TWFutures" not in markets for markets in BROKER_MARKETS.values())


@pytest.mark.parametrize("exchange_id", ["", "ibkr", "binance"])
def test_live_strategy_config_for_twfutures_is_rejected(exchange_id):
    with pytest.raises(ValueError, match="TWFutures.*not supported for live trading"):
        validate_strategy_config(
            exchange_id=exchange_id,
            market_category="TWFutures",
            market_type="swap",
            trade_direction="both",
            require_exchange=bool(exchange_id),
        )


# --- schedule -----------------------------------------------------------------------


def test_trading_days_use_xtai_and_taipei_daily_labels():
    assert CALENDAR_BY_MARKET["TWFutures"] == "XTAI"
    assert str(equity_bar_session_date(pd.Timestamp("2026-03-02 16:00"), "TWFutures")) == "2026-03-03"


@pytest.mark.parametrize(
    "closed_at, expected",
    [
        ("2026-03-02T09:00", True),   # Monday day session
        ("2026-03-02T13:45", True),   # day session close
        ("2026-03-02T14:00", False),  # between sessions
        ("2026-03-02T16:00", True),   # Monday night session
        ("2026-03-03T01:00", True),   # same night session after midnight
        ("2026-03-03T05:00", True),   # night session close
        ("2026-03-03T06:00", False),
        ("2026-03-07T01:00", True),   # Friday night runs into Saturday
        ("2026-03-07T09:00", False),  # Saturday
        ("2026-03-08T01:00", False),  # Saturday evening has no night session
    ],
)
def test_bar_close_gate_handles_the_night_session_across_midnight(closed_at, expected):
    assert is_equity_bar_close("TWFutures", _tp(closed_at)) is expected


def test_trading_day_completes_after_the_1345_day_close():
    before = latest_completed_session("TWFutures", _tp("2026-03-03T13:50"), data_delay_minutes=0)
    after = latest_completed_session("TWFutures", _tp("2026-03-03T13:46"), data_delay_minutes=0)
    assert before.date().isoformat() == "2026-03-03"
    assert after.date().isoformat() == "2026-03-03"
    earlier = latest_completed_session("TWFutures", _tp("2026-03-03T13:40"), data_delay_minutes=0)
    assert earlier.date().isoformat() == "2026-03-02"
    cutoff = equity_daily_bar_cutoff("TWFutures", _tp("2026-03-03T14:05"))
    assert pd.Timestamp("2026-03-02 16:00") <= cutoff  # 2026-03-03 Taipei label included


def test_intraday_bar_events_are_supported_and_daily_uses_sessions():
    candidates = [{"market": "TWFutures", "symbol": "TXF"}]
    assert supports_bar_close_events("1h", candidates) is True
    assert supports_bar_close_events("1d", candidates) is False


# --- limits / coverage --------------------------------------------------------------


def test_backtest_range_policy():
    for tf in ("1H", "4H", "1D", "1W"):
        assert backtest_range_policy("TWFutures", tf).max_days == 730
    assert backtest_range_policy("TAIFEX", "1d").max_days == 730
    summary = backtest_range_limit_summary(["TWFutures"])
    assert set(summary["markets"]) == {"TWFutures"}


def test_intraday_warmup_treats_twfutures_as_session_market():
    candidates = backtest_range_candidates([parse_instrument("TWFutures:TXF")])
    crypto = backtest_range_candidates([parse_instrument("Crypto:BTC/USDT")])
    assert backtest_warmup_days("5m", 60, candidates) > backtest_warmup_days("5m", 60, crypto)


def test_data_coverage_matches_backtest_rules():
    coverage = describe_data_coverage(
        market="TWFutures", symbol="TXF", timeframe="1h",
        start_date=datetime(2024, 1, 1), end_date=datetime(2026, 6, 30),
        earliest_lookup=lambda **_: None,
    )
    assert coverage["limits"]["validation_market"] == "TWFutures"
    assert coverage["limits"]["max_days"] == 730
    assert coverage["evaluation"]["feasible"] is False
    assert coverage["evaluation"]["range_check"]["violation"]["max_days"] == 730


def test_data_coverage_reports_the_first_stored_bar(monkeypatch):
    class Store:
        def earliest_bar_time_like(self, **kwargs):
            assert kwargs["symbol_pattern"] == "TXF__"
            return int(_tp("2026-03-02T08:45").timestamp())

    monkeypatch.setitem(DataSourceFactory._sources, "TWFutures", TWFuturesDataSource(kline_store=Store()))
    coverage = describe_data_coverage(
        market="TWFutures", symbol="TXF", timeframe="15m",
        start_date=datetime(2026, 3, 10), end_date=datetime(2026, 3, 20),
    )
    assert coverage["earliest_bar"]["timestamp"] == int(_tp("2026-03-02T08:45").timestamp())
    assert coverage["evaluation"]["history_check"]["status"] == "ok"


# --- contract rules -----------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol, multiplier, tick",
    [("TWFutures:TXF", 200, 1), ("TWFutures:TXFH6", 200, 1), ("TWFutures:MXF", 50, 1),
     ("TWFutures:TMF", 10, 1), ("TWFutures:EXF", 4000, 0.05), ("TWFutures:FXF", 1000, 0.2)],
)
def test_instrument_rules_carry_multiplier_and_tick(symbol, multiplier, tick):
    rules = default_rules_for_symbol(symbol)
    assert rules.contract_size == multiplier
    assert rules.amount_step == multiplier  # one contract is the minimum lot
    assert rules.price_tick == tick
    assert rules.source == "taifex_contract_spec"


def test_unknown_products_keep_engine_defaults():
    assert tw_futures_rules("TWFutures:ABC") is None
    assert tw_futures_rules("USStock:TXF") is None
    assert default_rules_for_symbol("TWFutures:ABC").amount_step == 1.0


# --- symbol catalog -----------------------------------------------------------------


class _FuturesTickers:
    def get_futopt_tickers(self, ticker_type="FUTURE"):
        assert ticker_type == "FUTURE"
        return [
            {"symbol": "TXFK6", "name": "臺股期貨115/11", "settlementDate": "2026-11-18"},
            {"symbol": "TXFL6", "name": "臺股期貨115/12", "settlementDate": "2026-12-16"},
            {"symbol": "MXFK6", "name": "小型臺指115/11", "settlementDate": "2026-11-18"},
            {"symbol": "TXFK6/L6", "name": "臺股期貨價差"},
            {"symbol": "TXO18000K6", "name": "臺指選擇權"},
        ]


def test_symbol_catalog_lists_products_and_contract_months():
    rows = fetch_tw_futures_symbols(_FuturesTickers())
    by_symbol = {row.symbol: row for row in rows}
    assert set(by_symbol) == {"TXF", "MXF", "TXFK6", "TXFL6", "MXFK6"}
    assert all(row.market == "TWFutures" and row.currency == "TWD" for row in rows)
    assert by_symbol["TXF"].name == "臺股期貨"
    assert by_symbol["TXF"].product_meta["continuous"] is True
    assert by_symbol["TXF"].product_meta["multiplier"] == 200
    assert by_symbol["TXFK6"].product_meta["settlementDate"] == "2026-11-18"
    assert by_symbol["MXF"].name == "小型臺指"
    assert FETCHERS["TWFutures"] is fetch_tw_futures_symbols


# --- strategy frame loading ---------------------------------------------------------


class _FakeFuturesSource:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls = []

    def get_kline(self, symbol, timeframe, limit, before_time=None, after_time=None):
        self.calls.append((symbol, timeframe))
        if self.error:
            raise self.error
        return [dict(row) for row in self.rows]


@pytest.fixture
def _no_frame_cache(monkeypatch):
    from app.services.strategy_v2 import market_data

    market_data.clear_shared_strategy_frame_cache()
    monkeypatch.setattr(market_data._cache, "get", lambda _key: None)
    monkeypatch.setattr(market_data._cache, "put", lambda *_args: None)
    yield market_data
    market_data.clear_shared_strategy_frame_cache()


def test_strategy_frame_passes_the_session_data_symbol(monkeypatch, _no_frame_cache):
    rows = [
        {"time": int(_tp(f"2026-03-02T{hour:02d}:00").timestamp()), "open": 1, "high": 1, "low": 1,
         "close": 1, "volume": 1}
        for hour in (9, 10, 11)
    ]
    source = _FakeFuturesSource(rows)
    monkeypatch.setitem(DataSourceFactory._sources, "TWFutures", source)
    frame = _no_frame_cache.load_strategy_frame(
        "TWFutures", "TXF@regular", "1h", datetime(2026, 3, 2), datetime(2026, 3, 2, 23),
    )
    assert len(frame) == 3
    assert source.calls[0] == ("TXF@regular", "1H")


def test_strategy_frame_reports_the_sidecar_failure(monkeypatch, _no_frame_cache):
    from app.data_sources.errors import MarketDataUnavailableError
    from app.data_sources.tw_stock import FubonSidecarError

    source = _FakeFuturesSource(error=FubonSidecarError(
        "Fubon market data sidecar at http://fubon-marketdata:8010 is unreachable."
    ))
    monkeypatch.setitem(DataSourceFactory._sources, "TWFutures", source)
    with pytest.raises(MarketDataUnavailableError) as excinfo:
        _no_frame_cache.load_strategy_frame(
            "TWFutures", "TXF", "1h", datetime(2026, 3, 2), datetime(2026, 3, 2, 23),
        )
    assert "sidecar" in excinfo.value.failure.technical_detail


# --- backtests ----------------------------------------------------------------------


SYMBOL = "TWFutures:TXF"


def _hourly_frames():
    """Hourly TXF bars: day session 13:00 then night 15:00-19:00, then the next day session.

    The night session rallies 50 points per bar, the day session 10; the
    regular-session frame is the same bars without the night session.
    """
    bars = [
        ("2026-03-02 13:00", 20000.0),
        ("2026-03-02 15:00", 20010.0), ("2026-03-02 16:00", 20060.0), ("2026-03-02 17:00", 20110.0),
        ("2026-03-02 18:00", 20160.0), ("2026-03-02 19:00", 20210.0),
        ("2026-03-03 08:00", 20220.0), ("2026-03-03 09:00", 20230.0), ("2026-03-03 10:00", 20240.0),
        ("2026-03-03 11:00", 20250.0), ("2026-03-03 12:00", 20260.0), ("2026-03-03 13:00", 20270.0),
    ]
    stamps = [stamp for stamp, _ in bars]
    index = pd.DatetimeIndex(
        [pd.Timestamp(item, tz="Asia/Taipei").tz_convert("UTC").tz_localize(None) for item in stamps]
    )
    prices = [price for _, price in bars]
    frame = pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1_000_000] * len(stamps)},
        index=index,
    )
    local_hours = [int(item[-5:-3]) for item in stamps]
    regular = frame[[8 <= hour <= 13 for hour in local_hours]]
    return frame, regular


_HOLD_STRATEGY = f"""
def initialize(context):
    g.symbol = "{SYMBOL}"
    g.bar = 0
    context.set_universe([g.symbol])
    context.subscribe(frequency="1h")
    context.set_metadata(direction_mode="neutral")

def handle_data(context, data):
    if g.bar == 0:
        order(g.symbol, 200, reason="entry")
    elif g.bar == 3:
        order(g.symbol, -200, reason="exit")
    g.bar += 1
"""


class _NoRules:
    def historical_snapshot(self, *_args, **_kwargs):
        raise AssertionError("TWFutures does not use exchange rule snapshots")


def _service(calls):
    frame_all, frame_regular = _hourly_frames()

    def fetch(market, symbol, timeframe, *_args, **_kwargs):
        calls.append((market, symbol, timeframe))
        assert market == "TWFutures"
        return (frame_regular if symbol == "TXF@regular" else frame_all).copy()

    return StrategyV2BacktestService(repository=object(), frame_fetcher=fetch, instrument_rules_provider=_NoRules())


def _run_backtest(session=None):
    calls = []
    kwargs = {"session": session} if session else {}
    _, result = _service(calls).run(
        user_id=1,
        code=_HOLD_STRATEGY,
        start_date=datetime(2026, 3, 2),
        end_date=datetime(2026, 3, 3, 23, 59, 59),
        initial_capital=10_000_000,
        commission=0,
        slippage=0,
        persist=False,
        **kwargs,
    )
    return result, calls


def test_backtest_pnl_follows_the_contract_multiplier_with_night_session():
    result, calls = _run_backtest()

    assert {call[1] for call in calls} == {"TXF"}
    trade = result["closedTrades"][0]
    # One contract: entry fills at bar 1 open (15:00 night, 20010), exit at bar 4
    # open (18:00 night, 20160); 150 points x 200 TWD.
    assert trade["quantity"] == 200
    assert (trade["entry_price"], trade["exit_price"]) == (20010, 20160)
    assert trade["profit"] == pytest.approx(30_000)
    assumptions = result["executionAssumptions"]["twFutures"]
    assert assumptions["session"] == "all"
    assert assumptions["rollMethod"] == "front_month_unadjusted"
    assert assumptions["contracts"]["TXF"]["contractMultiplier"] == 200


def test_backtest_regular_session_skips_the_night_and_differs():
    regular, calls = _run_backtest("regular")
    full, _ = _run_backtest("all")

    assert {call[1] for call in calls} == {"TXF@regular"}
    trade = regular["closedTrades"][0]
    # Day session only: entry at 08:00 next day (20220), exit at 11:00 (20250).
    assert (trade["entry_price"], trade["exit_price"]) == (20220, 20250)
    assert trade["profit"] == pytest.approx(200 * 30)
    assert regular["executionAssumptions"]["twFutures"]["session"] == "regular"
    assert full["closedTrades"][0]["profit"] == pytest.approx(30_000)
    assert trade["profit"] != full["closedTrades"][0]["profit"]


def test_backtest_rejects_a_partial_contract():
    code = _HOLD_STRATEGY.replace("order(g.symbol, 200,", "order(g.symbol, 100,")
    _, result = _service([]).run(
        user_id=1, code=code, start_date=datetime(2026, 3, 2), end_date=datetime(2026, 3, 3, 23, 59, 59),
        initial_capital=10_000_000, commission=0, slippage=0, persist=False,
    )
    assert result["closedTrades"] == []
    assert result.get("totalExecutions", 0) == 0


def test_backtest_rejects_unknown_session():
    from app.services.strategy_v2.contract import StrategyV2ContractError

    with pytest.raises(StrategyV2ContractError, match="twFuturesSessionUnsupported"):
        _run_backtest("night")
