"""Perpetual funding simulation for Strategy API V2 backtests (issue #31)."""

from datetime import datetime

import pandas as pd
import pytest

from app.services.funding_rates import (
    BinanceUsdmFundingRateClient,
    FundingHistory,
    FundingRateHistoryService,
    FundingRateRecord,
    FundingRateUnsupportedError,
    backfill_windows,
    binance_usdm_symbol,
    missing_funding_periods,
)
from app.services.strategy_v2 import StrategyV2BacktestRunner, StrategyV2ContractError
from app.services.strategy_v2.funding import (
    FUNDING_MODE_HISTORICAL,
    FUNDING_MODE_NOT_MODELED,
    build_funding_plan,
    normalize_funding_mode,
)
from app.services.strategy_v2.service import StrategyV2BacktestService
from app.services.strategy_v2.storage import compact_backtest_result


SWAP = "Crypto:BTC/USDT@swap"
T0 = pd.Timestamp("2026-01-01 00:00")


def _ts(hours: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(hours=hours)


def _flat_frame(bars: int = 6, price: float = 100.0) -> pd.DataFrame:
    index = pd.date_range(T0, periods=bars, freq="4h")
    return pd.DataFrame(
        {
            "open": [price] * bars,
            "high": [price] * bars,
            "low": [price] * bars,
            "close": [price] * bars,
            "volume": [1_000_000] * bars,
        },
        index=index,
    )


def _hold_strategy(side: str, quantity: float = 10) -> str:
    """Open on bar 0 (fills at bar 1 open 04:00) and close on bar 3 (fills at bar 4 open 16:00)."""
    sign = 1 if side == "long" else -1
    return f"""
def initialize(context):
    g.symbol = "{SWAP}"
    g.bar = 0
    context.set_universe([g.symbol])
    context.subscribe(frequency="4h")
    context.set_metadata(direction_mode="neutral")

def handle_data(context, data):
    if g.bar == 0:
        order(g.symbol, {sign * quantity}, position_side="{side}", reason="entry")
    elif g.bar == 3:
        order(g.symbol, {-sign * quantity}, position_side="{side}", reason="exit")
    g.bar += 1
"""


def _run(code: str, funding_rates=None, frame: pd.DataFrame | None = None) -> dict:
    kwargs = {}
    if funding_rates is not None:
        kwargs["funding_rates"] = funding_rates
    return StrategyV2BacktestRunner(
        code=code,
        frames={SWAP: frame if frame is not None else _flat_frame()},
        initial_capital=10_000,
        commission=0,
        slippage=0,
        **kwargs,
    ).run()


FUNDING_RESULT_KEYS = {
    "totalFundingFee",
    "fundingFeePaid",
    "fundingFeeReceived",
    "closedTradeFundingFee",
    "openPositionFundingFee",
    "fundingEventCount",
    "fundingEvents",
}


# --- simulation -----------------------------------------------------------


def _four_hour_rates(rate: float) -> list[tuple[pd.Timestamp, float]]:
    # A four-hour cadence: settlements 00:00 through 20:00.
    return [(_ts(hour), rate) for hour in range(0, 24, 4)]


def test_long_pays_positive_rate_across_multiple_four_hour_settlements():
    result = _run(_hold_strategy("long"), {SWAP: _four_hour_rates(0.0001)})

    # Held from the 04:00 open to the 16:00 open: settlements 08:00, 12:00 and
    # 16:00 (the 16:00 settlement closes bar 3 while the position is open).
    events = result["fundingEvents"]
    assert [item["time"] for item in events] == [
        "2026-01-01T08:00:00Z",
        "2026-01-01T12:00:00Z",
        "2026-01-01T16:00:00Z",
    ]
    assert all(item["side"] == "long" for item in events)
    assert all(item["payment"] == pytest.approx(0.1) for item in events)

    trade = result["closedTrades"][0]
    assert trade["funding_fee"] == pytest.approx(0.3)
    assert trade["profit_before_funding"] == pytest.approx(0.0)
    assert trade["profit"] == pytest.approx(-0.3)
    assert result["totalFundingFee"] == pytest.approx(0.3)
    assert result["fundingFeePaid"] == pytest.approx(0.3)
    assert result["fundingFeeReceived"] == pytest.approx(0.0)
    assert result["closedTradeFundingFee"] == pytest.approx(0.3)
    assert result["openPositionFundingFee"] == pytest.approx(0.0)
    assert result["fundingEventCount"] == 3
    assert result["finalEquity"] == pytest.approx(10_000 - 0.3)
    assert result["totalProfit"] == pytest.approx(-0.3)
    assert result["audit"]["passed"] is True
    assert result["audit"]["fundingLedger"] == pytest.approx(0.3)
    assert "funding" in result["audit"]["scope"]


@pytest.mark.parametrize(
    ("side", "rate", "expected_fee"),
    [
        ("short", 0.0001, -0.3),   # short receives a positive rate
        ("long", -0.0001, -0.3),   # long receives a negative rate
        ("short", -0.0001, 0.3),   # short pays a negative rate
    ],
)
def test_funding_direction_follows_position_side_and_rate_sign(side, rate, expected_fee):
    result = _run(_hold_strategy(side), {SWAP: _four_hour_rates(rate)})

    trade = result["closedTrades"][0]
    assert trade["side"] == side
    assert trade["funding_fee"] == pytest.approx(expected_fee)
    assert trade["profit"] == pytest.approx(-expected_fee)
    assert result["totalFundingFee"] == pytest.approx(expected_fee)
    assert result["finalEquity"] == pytest.approx(10_000 - expected_fee)
    if expected_fee < 0:
        assert result["fundingFeeReceived"] == pytest.approx(-expected_fee)
        assert result["fundingFeePaid"] == pytest.approx(0.0)
    else:
        assert result["fundingFeePaid"] == pytest.approx(expected_fee)
    assert result["audit"]["passed"] is True


def test_hourly_settlement_cadence_is_taken_from_the_data():
    hourly = [(_ts(hour), 0.0001) for hour in range(0, 24)]
    result = _run(_hold_strategy("long"), {SWAP: hourly})

    # Settlements 05:00 through 16:00 fall inside the held bars.
    assert result["fundingEventCount"] == 12
    assert result["closedTrades"][0]["funding_fee"] == pytest.approx(1.2)


def test_mixed_rates_settle_at_the_settlement_bar_close():
    frame = _flat_frame()
    frame.loc[_ts(8), "close"] = 110.0  # bar 08:00..12:00 closes at 110
    rates = [(_ts(8), 0.0002), (_ts(12), -0.0001), (_ts(16), 0.0003)]
    result = _run(_hold_strategy("long"), {SWAP: rates}, frame=frame)

    payments = [item["payment"] for item in result["fundingEvents"]]
    # 08:00 settles at bar 04:00 close (100); 12:00 at bar 08:00 close (110).
    assert payments == pytest.approx([10 * 100 * 0.0002, 10 * 110 * -0.0001, 10 * 100 * 0.0003])
    assert result["closedTrades"][0]["funding_fee"] == pytest.approx(sum(payments))
    assert result["audit"]["passed"] is True


def test_missing_settlements_count_as_zero():
    rates = [(_ts(8), 0.0001), (_ts(16), 0.0001)]  # 12:00 settlement missing
    result = _run(_hold_strategy("long"), {SWAP: rates})

    assert result["fundingEventCount"] == 2
    assert result["closedTrades"][0]["funding_fee"] == pytest.approx(0.2)


def test_open_position_funding_is_reported_and_deducted_from_equity():
    code = f"""
def initialize(context):
    g.symbol = "{SWAP}"
    g.sent = False
    context.set_universe([g.symbol])
    context.subscribe(frequency="4h")
    context.set_metadata(direction_mode="neutral")

def handle_data(context, data):
    if not g.sent:
        order(g.symbol, 10, position_side="long")
        g.sent = True
"""
    result = _run(code, {SWAP: _four_hour_rates(0.0001)})

    assert result["closedTrades"] == []
    # Held from 04:00; settlements 08:00..20:00 and the bar 20:00 span ends at 24:00.
    assert result["fundingEventCount"] == 4
    assert result["openPositionFundingFee"] == pytest.approx(0.4)
    assert result["closedTradeFundingFee"] == pytest.approx(0.0)
    assert result["finalEquity"] == pytest.approx(10_000 - 0.4)
    row = result["attribution"]["symbols"][0]
    assert row["fundingFee"] == pytest.approx(0.4)
    assert row["netContribution"] == pytest.approx(-0.4 / 10_000)
    assert result["audit"]["passed"] is True


def test_partial_close_allocates_accrued_funding_pro_rata():
    code = f"""
def initialize(context):
    g.symbol = "{SWAP}"
    g.bar = 0
    context.set_universe([g.symbol])
    context.subscribe(frequency="4h")
    context.set_metadata(direction_mode="neutral")

def handle_data(context, data):
    if g.bar == 0:
        order(g.symbol, 10, position_side="long")
    elif g.bar == 2:
        order(g.symbol, -4, position_side="long")
    elif g.bar == 3:
        order(g.symbol, -6, position_side="long")
    g.bar += 1
"""
    result = _run(code, {SWAP: _four_hour_rates(0.0001)})

    first, second = result["closedTrades"]
    # 08:00 and 12:00 on 10 units (0.2) -> 40% to the first partial close.
    assert first["funding_fee"] == pytest.approx(0.08)
    # Remaining 0.12 plus 16:00 on 6 units (0.06).
    assert second["funding_fee"] == pytest.approx(0.18)
    assert result["totalFundingFee"] == pytest.approx(0.26)
    assert result["audit"]["passed"] is True


def test_default_off_result_is_identical_to_status_quo():
    code = _hold_strategy("long")
    baseline = _run(code)
    explicit_none = StrategyV2BacktestRunner(
        code=code,
        frames={SWAP: _flat_frame()},
        initial_capital=10_000,
        commission=0,
        slippage=0,
        funding_rates=None,
    ).run()

    assert explicit_none == baseline
    assert not FUNDING_RESULT_KEYS & set(baseline)
    trade = baseline["closedTrades"][0]
    assert "funding_fee" not in trade
    assert "profit_before_funding" not in trade
    assert "fundingLedger" not in baseline["audit"]
    assert "funding" not in baseline["audit"]["scope"]
    assert all("fundingFee" not in row for row in baseline["attribution"]["symbols"])
    assert "fundingEvents" not in compact_backtest_result(baseline)


def test_enabled_without_settlements_changes_only_funding_fields():
    code = _hold_strategy("long")
    baseline = _run(code)
    enabled = _run(code, {SWAP: []})

    assert enabled["equityCurve"] == baseline["equityCurve"]
    assert enabled["finalEquity"] == baseline["finalEquity"]
    assert enabled["totalFundingFee"] == 0.0
    assert enabled["closedTrades"][0]["profit"] == baseline["closedTrades"][0]["profit"]


# --- missing-data detection ----------------------------------------------


def test_missing_periods_detect_gaps_using_local_cadence():
    times = [_ts(hour) for hour in (0, 8, 16, 40, 48)]  # 24:00 and 32:00 missing
    periods = missing_funding_periods(times, _ts(0), _ts(48))

    assert periods == [{
        "start": "2026-01-01T16:00:00Z",
        "end": "2026-01-02T16:00:00Z",
        "missingCount": 2,
        "reason": "gap",
    }]


def test_missing_periods_do_not_flag_a_cadence_change():
    # Eight-hour settlements followed by four-hour settlements.
    times = [_ts(hour) for hour in (0, 8, 16, 24, 28, 32, 36)]
    assert missing_funding_periods(times, _ts(0), _ts(36)) == []


def test_missing_periods_report_range_edges_and_empty_history():
    times = [_ts(hour) for hour in (16, 20, 24)]  # four-hour cadence
    periods = missing_funding_periods(times, _ts(0), _ts(32))

    assert periods[0] == {
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T16:00:00Z",
        "missingCount": 4,
        "reason": "range_start",
    }
    assert periods[-1] == {
        "start": "2026-01-02T00:00:00Z",
        "end": "2026-01-02T08:00:00Z",
        "missingCount": 2,
        "reason": "range_end",
    }
    assert missing_funding_periods([], _ts(0), _ts(24)) == [{
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
        "missingCount": None,
        "reason": "no_funding_data",
    }]


def test_backfill_windows_cover_only_missing_ranges():
    stored = [_ts(hour) for hour in (0, 8, 16, 40, 48)]
    assert backfill_windows(stored, _ts(0), _ts(48)) == [(_ts(16), _ts(40))]
    assert backfill_windows([], _ts(0), _ts(48)) == [(_ts(0), _ts(48))]


# --- data source and storage ---------------------------------------------


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_binance_client_paginates_and_parses_settlements():
    calls = []
    first_page_start = int(_ts(0).tz_localize("UTC").value // 1_000_000)

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params)))
        if len(calls) == 1:
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": first_page_start + index * 3_600_000,
                    "fundingRate": "0.00010000",
                    "markPrice": "100.0",
                }
                for index in range(1000)
            ]
        else:
            rows = [{
                "symbol": "BTCUSDT",
                "fundingTime": first_page_start + 1000 * 3_600_000,
                "fundingRate": "-0.00005000",
                "markPrice": "",
            }]
        return _Response(rows)

    client = BinanceUsdmFundingRateClient(http_get=fake_get)
    records = client.fetch("BTCUSDT", _ts(0).to_pydatetime(), _ts(2000).to_pydatetime())

    assert len(calls) == 2
    assert calls[0][0] == "https://fapi.binance.com/fapi/v1/fundingRate"
    assert calls[0][1]["symbol"] == "BTCUSDT"
    assert calls[0][1]["limit"] == 1000
    assert calls[1][1]["startTime"] == first_page_start + 999 * 3_600_000 + 1
    assert len(records) == 1001
    assert records[0].funding_time == _ts(0)
    assert records[0].rate == pytest.approx(0.0001)
    assert records[0].mark_price == pytest.approx(100.0)
    assert records[-1].rate == pytest.approx(-0.00005)
    assert records[-1].mark_price is None


class _MemoryRepository:
    def __init__(self, records=()):
        self.rows = {(item.exchange_id, item.symbol, item.funding_time): item for item in records}
        self.upserts = []

    def load(self, exchange_id, symbol, start, end):
        return sorted(
            (
                item for (exchange, venue_symbol, time), item in self.rows.items()
                if exchange == exchange_id and venue_symbol == symbol and start <= time <= end
            ),
            key=lambda item: item.funding_time,
        )

    def upsert(self, records, *, source):
        records = list(records)
        self.upserts.append((records, source))
        for item in records:
            self.rows[(item.exchange_id, item.symbol, item.funding_time)] = item
        return len(records)


class _FakeClient:
    def __init__(self, available, error=None):
        self.available = available
        self.error = error
        self.calls = []

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
        if self.error:
            raise self.error
        return [
            item for item in self.available
            if pd.Timestamp(start) <= item.funding_time <= pd.Timestamp(end)
        ]


def _records(hours, rate=0.0001):
    return [FundingRateRecord("binance", "BTCUSDT", _ts(hour), rate) for hour in hours]


def test_history_service_backfills_missing_window_and_persists_deduplicated_rows():
    repository = _MemoryRepository(_records((0, 8, 16, 40, 48)))
    client = _FakeClient(_records(range(0, 49, 8)))
    service = FundingRateHistoryService(repository=repository, clients={"binance": client})

    history = service.history(exchange_id="binance", symbol="BTCUSDT", start=_ts(0), end=_ts(48))

    assert client.calls == [("BTCUSDT", _ts(16), _ts(40))]
    assert [item.funding_time for item in history.records] == [_ts(hour) for hour in range(0, 49, 8)]
    assert history.missing_periods == []
    assert len(repository.rows) == 7  # 16:00 and 40:00 re-fetched but deduplicated
    assert repository.upserts[0][1] == "binance_usdm_funding_rate"

    # A second run finds complete stored data and makes no request.
    service.history(exchange_id="binance", symbol="BTCUSDT", start=_ts(0), end=_ts(48))
    assert len(client.calls) == 1


def test_history_service_reports_missing_data_when_backfill_fails():
    repository = _MemoryRepository(_records((0, 8, 16, 40, 48)))
    client = _FakeClient([], error=RuntimeError("exchange unavailable"))
    service = FundingRateHistoryService(repository=repository, clients={"binance": client})

    history = service.history(exchange_id="binance", symbol="BTCUSDT", start=_ts(0), end=_ts(48))

    assert history.fetch_errors == ["exchange unavailable"]
    assert history.missing_periods[0]["missingCount"] == 2
    assert len(history.records) == 5


def test_history_service_rejects_unsupported_exchange():
    service = FundingRateHistoryService(repository=_MemoryRepository(), clients={"binance": _FakeClient([])})
    with pytest.raises(FundingRateUnsupportedError):
        service.history(exchange_id="okx", symbol="BTCUSDT", start=_ts(0), end=_ts(8))


def test_binance_usdm_symbol_mapping():
    assert binance_usdm_symbol("BTC/USDT") == "BTCUSDT"
    assert binance_usdm_symbol("ETH/USDC") == "ETHUSDC"
    assert binance_usdm_symbol("SOLUSDT") == "SOLUSDT"
    with pytest.raises(FundingRateUnsupportedError):
        binance_usdm_symbol("BTC/USD")  # COIN-M is not supported


# --- mode option and plan -------------------------------------------------


def test_funding_mode_defaults_to_not_modeled_and_rejects_unknown_values():
    assert normalize_funding_mode(None) == FUNDING_MODE_NOT_MODELED
    assert normalize_funding_mode("") == FUNDING_MODE_NOT_MODELED
    assert normalize_funding_mode("Historical_Rates") == FUNDING_MODE_HISTORICAL
    with pytest.raises(StrategyV2ContractError, match="fundingModeUnsupported"):
        normalize_funding_mode("fixed_rate")


class _Provider:
    def __init__(self, histories):
        self.histories = histories
        self.calls = []

    def supports(self, exchange_id):
        return exchange_id == "binance"

    def history(self, *, exchange_id, symbol, start, end):
        self.calls.append((exchange_id, symbol, start, end))
        return self.histories[symbol]


def test_funding_plan_marks_spot_and_unsupported_venues_not_modeled():
    provider = _Provider({
        "BTCUSDT": FundingHistory(
            exchange_id="binance",
            symbol="BTCUSDT",
            records=_records((0, 8)),
            missing_periods=[{"start": "a", "end": "b", "missingCount": 3, "reason": "gap"}],
        ),
    })
    candidates = [
        {"key": "Crypto:BTC/USDT@binance:swap", "market": "Crypto", "symbol": "BTC/USDT",
         "exchange_id": "binance", "market_type": "swap"},
        {"key": "Crypto:ETH/USDT@okx:swap", "market": "Crypto", "symbol": "ETH/USDT",
         "exchange_id": "okx", "market_type": "swap"},
        {"key": "Crypto:BTC/USDT@binance:spot", "market": "Crypto", "symbol": "BTC/USDT",
         "exchange_id": "binance", "market_type": "spot"},
    ]
    plan = build_funding_plan(
        candidates,
        frame_keys=[item["key"] for item in candidates],
        start=datetime(2026, 1, 1),
        end=datetime(2026, 1, 2),
        provider=provider,
    )

    assert set(plan.rates) == {"Crypto:BTC/USDT@binance:swap"}
    statuses = {item["instrument"]: item["status"] for item in plan.instruments}
    assert statuses == {
        "Crypto:BTC/USDT@binance:swap": "modeled",
        "Crypto:ETH/USDT@okx:swap": "unsupported_exchange",
        "Crypto:BTC/USDT@binance:spot": "not_applicable",
    }
    report = plan.report(FUNDING_MODE_HISTORICAL)
    assert report["missingPeriodCount"] == 1
    assert report["missingSettlementCount"] == 3
    assert "strategyV2.fundingDataMissing:Crypto:BTC/USDT@binance:swap" in report["warnings"]
    assert "strategyV2.fundingNotModeled:Crypto:ETH/USDT@okx:swap:unsupported_exchange" in report["warnings"]


# --- service integration --------------------------------------------------


class _RulesProvider:
    def historical_snapshot(self, *_args, **_kwargs):
        return None


def _service(provider=None):
    def fetch(*_args, **_kwargs):
        return _flat_frame(bars=12)

    return StrategyV2BacktestService(
        repository=object(),
        frame_fetcher=fetch,
        instrument_rules_provider=_RulesProvider(),
        funding_rate_provider=provider,
    )


def _service_run(service, **kwargs):
    return service.run(
        user_id=1,
        code=_hold_strategy("long"),
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 2, 23, 59, 59),
        initial_capital=10_000,
        commission=0,
        slippage=0,
        persist=False,
        **kwargs,
    )[1]


def test_service_default_keeps_funding_not_modeled():
    provider = _Provider({})
    result = _service_run(_service(provider))

    assert result["executionAssumptions"]["fundingMode"] == "not_modeled"
    assert "fundingPriceBasis" not in result["executionAssumptions"]
    assert "funding" not in result
    assert not FUNDING_RESULT_KEYS & set(result)
    assert provider.calls == []


def test_service_historical_mode_reports_funding_and_missing_periods():
    history = FundingHistory(
        exchange_id="binance",
        symbol="BTCUSDT",
        records=_records((8, 16)),
        missing_periods=[{
            "start": "2026-01-01T08:00:00Z",
            "end": "2026-01-01T16:00:00Z",
            "missingCount": 1,
            "reason": "gap",
        }],
    )
    provider = _Provider({"BTCUSDT": history})
    result = _service_run(_service(provider), funding_mode="historical_rates")

    assert provider.calls[0][:2] == ("binance", "BTCUSDT")
    assert result["executionAssumptions"]["fundingMode"] == "historical_rates"
    assert result["executionAssumptions"]["fundingPriceBasis"] == "settlement_bar_close"
    funding = result["funding"]
    assert funding["mode"] == "historical_rates"
    assert funding["missingPeriodCount"] == 1
    assert funding["missingSettlementCount"] == 1
    assert funding["instruments"][0]["status"] == "modeled"
    assert funding["warnings"] == [f"strategyV2.fundingDataMissing:{SWAP}"]
    assert result["totalFundingFee"] == pytest.approx(0.2)
    assert result["closedTrades"][0]["funding_fee"] == pytest.approx(0.2)
    assert result["audit"]["passed"] is True
