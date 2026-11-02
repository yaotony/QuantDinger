"""TWFutures data source: stitching, sessions, daily bars and sidecar merge (issue #57).

PostgreSQL (``qd_tw_klines``) and the Fubon sidecar are replaced by fakes; the
trading calendar and the clock are injected.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import pytest

from app.data_sources.tw_futures import (
    TWFuturesDataSource,
    aggregate_daily,
    bar_session,
    normalize_session,
    parse_tw_futures_symbol,
    trading_day_of,
    tw_futures_data_symbol,
)
from app.data_sources.tw_stock import FubonSidecarError

TAIPEI = ZoneInfo("Asia/Taipei")


def _ts(day: str, clock: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI).timestamp())


def _bars(day: str, clock: str, count: int, price: float, step: float = 0.0) -> List[Dict[str, Any]]:
    start = _ts(day, clock)
    return [
        {
            "time": start + 60 * i,
            "open": price + step * i,
            "high": price + step * i + 1,
            "low": price + step * i - 1,
            "close": price + step * i,
            "volume": 1.0,
        }
        for i in range(count)
    ]


def weekdays(*holidays: str):
    closed = {date.fromisoformat(item) for item in holidays}
    return lambda day: day.weekday() < 5 and day not in closed


class FakeStore:
    """In-memory ``qd_tw_klines`` keyed by (contract, session)."""

    def __init__(
        self,
        rows: Dict[Tuple[str, str], List[Dict[str, Any]]] | None = None,
        daily: Dict[Tuple[str, str], List[Dict[str, Any]]] | None = None,
    ) -> None:
        self.rows = rows or {}
        self.daily = daily or {}
        self.calls: List[Tuple[str, str, int, int]] = []
        self.earliest_calls: List[Dict[str, Any]] = []
        self.earliest: int | None = None
        self.earliest_daily: int | None = None

    def load_bars(self, *, market, symbol, session, timeframe, start_ts=None, end_ts=None, limit=None):
        assert market == "TWFutures" and timeframe in ("1m", "1D")
        source = self.rows if timeframe == "1m" else self.daily
        if timeframe == "1m":
            self.calls.append((symbol, session, start_ts, end_ts))
        return [
            dict(bar) for bar in source.get((symbol, session), [])
            if (start_ts is None or bar["time"] >= start_ts) and (end_ts is None or bar["time"] < end_ts)
        ]

    def earliest_bar_time_like(self, *, market, symbol_pattern, sessions, timeframe):
        if timeframe == "1D":
            return self.earliest_daily
        self.earliest_calls.append({"pattern": symbol_pattern, "sessions": tuple(sessions)})
        return self.earliest


def _source(store: FakeStore, *, now: str = "2026-12-01T10:00", holidays=(), sidecar=None) -> TWFuturesDataSource:
    day, clock = now.split("T")
    source = TWFuturesDataSource(
        kline_store=store,
        is_trading_day=weekdays(*holidays),
        clock=lambda: float(_ts(day, clock)),
    )
    calls: List[Tuple[str, str]] = []

    def fake_intraday(contract, session):
        calls.append((contract, session))
        value = (sidecar or {}).get((contract, session), [])
        if isinstance(value, Exception):
            raise value
        return [dict(row) for row in value]

    source.fetch_futopt_minute_bars = fake_intraday  # type: ignore[assignment]
    source.sidecar_calls = calls  # type: ignore[attr-defined]
    return source


def _prices(klines):
    return [(datetime.fromtimestamp(k["time"], tz=TAIPEI).strftime("%m-%d %H:%M"), k["close"]) for k in klines]


# --- symbols and sessions -------------------------------------------------------------


def test_parse_symbols():
    assert parse_tw_futures_symbol("TWFutures:TXF").contract == ""
    assert parse_tw_futures_symbol("txfh6").contract == "TXFH6"
    assert parse_tw_futures_symbol("TXFH6").product == "TXF"
    assert parse_tw_futures_symbol("TXF@regular").session == "regular"
    assert parse_tw_futures_symbol("TXF").session == "all"
    for invalid in ("", "TX", "TXFM6", "TXF@night", "2330.TW"):
        assert parse_tw_futures_symbol(invalid) is None


def test_session_option_and_data_symbol():
    assert normalize_session(None) == "all"
    assert normalize_session("Regular") == "regular"
    with pytest.raises(ValueError, match="twFuturesSessionUnsupported:night"):
        normalize_session("night")
    assert tw_futures_data_symbol("TXF", "all") == "TXF"
    assert tw_futures_data_symbol("TXF", "regular") == "TXF@regular"
    assert tw_futures_data_symbol("TXF@regular", "all") == "TXF"


def test_night_session_belongs_to_the_next_trading_day():
    cal = weekdays()
    assert bar_session(_ts("2026-11-20", "09:00")) == "regular"
    assert bar_session(_ts("2026-11-20", "15:00")) == "afterhours"
    assert bar_session(_ts("2026-11-21", "04:59")) == "afterhours"
    assert bar_session(_ts("2026-11-20", "14:30")) is None
    # Monday night -> Tuesday; Friday night (including after midnight) -> Monday.
    assert trading_day_of(_ts("2026-11-16", "21:00"), cal) == date(2026, 11, 17)
    assert trading_day_of(_ts("2026-11-20", "15:00"), cal) == date(2026, 11, 23)
    assert trading_day_of(_ts("2026-11-21", "01:00"), cal) == date(2026, 11, 23)
    assert trading_day_of(_ts("2026-11-20", "09:00"), cal) == date(2026, 11, 20)
    # A Monday holiday moves Friday's night session to Tuesday.
    assert trading_day_of(_ts("2026-11-20", "15:00"), weekdays("2026-11-23")) == date(2026, 11, 24)


# --- continuous stitching -------------------------------------------------------------


def _roll_store() -> FakeStore:
    return FakeStore({
        ("TXFK6", "regular"): _bars("2026-11-17", "09:00", 1, 90) + _bars("2026-11-18", "13:00", 30, 100),
        ("TXFL6", "regular"): _bars("2026-11-17", "09:00", 1, 190) + _bars("2026-11-18", "13:00", 45, 200),
        ("TXFL6", "afterhours"): _bars("2026-11-18", "15:00", 2, 210),
        ("TXFK6", "afterhours"): _bars("2026-11-17", "15:00", 1, 95),
    })


def test_continuous_code_switches_contract_at_1330_on_expiry_day():
    store = _roll_store()
    source = _source(store)
    klines = source.get_kline(
        "TXF", "1m", 1000,
        after_time=_ts("2026-11-17", "00:00"), before_time=_ts("2026-11-19", "00:00"),
    )
    prices = _prices(klines)
    assert prices[0] == ("11-17 09:00", 90)  # near month K6
    assert ("11-17 15:00", 95) in prices  # K6 night session before expiry day
    assert ("11-18 13:29", 100) in prices  # K6 still trades its last day session until 13:30
    assert ("11-18 13:30", 200) in prices  # L6 takes over at 13:30
    assert ("11-18 13:44", 200) in prices
    assert prices[-2:] == [("11-18 15:00", 210), ("11-18 15:01", 210)]  # night session trades L6
    assert not any(price == 190 for _, price in prices)  # L6 is not used before the roll
    assert {call[0] for call in store.calls} == {"TXFK6", "TXFL6"}
    assert source.sidecar_calls == []  # historical window: no sidecar call


def test_explicit_contract_is_not_stitched():
    source = _source(_roll_store())
    klines = source.get_kline(
        "TXFL6", "1m", 1000,
        after_time=_ts("2026-11-17", "00:00"), before_time=_ts("2026-11-19", "00:00"),
    )
    assert {price for _, price in _prices(klines)} == {190, 200, 210}


def test_regular_session_drops_night_bars():
    source = _source(_roll_store())
    klines = source.get_kline(
        "TXF@regular", "1m", 1000,
        after_time=_ts("2026-11-17", "00:00"), before_time=_ts("2026-11-19", "00:00"),
    )
    prices = _prices(klines)
    assert ("11-17 15:00", 95) not in prices
    assert all(not stamp.endswith(("15:00", "15:01")) for stamp, _ in prices)
    assert ("11-18 13:30", 200) in prices


def test_hourly_bars_are_resampled_from_stitched_minutes():
    source = _source(_roll_store())
    klines = source.get_kline(
        "TXF", "1H", 100,
        after_time=_ts("2026-11-18", "00:00"), before_time=_ts("2026-11-19", "00:00"),
    )
    by_time = {stamp: k for stamp, k in zip((s for s, _ in _prices(klines)), klines)}
    bar = by_time["11-18 13:00"]
    assert bar["open"] == 100 and bar["close"] == 200
    assert bar["volume"] == 45  # 30 K6 minutes + 15 L6 minutes
    assert by_time["11-18 15:00"]["close"] == 210


# --- daily bars ----------------------------------------------------------------------


def _week_store() -> FakeStore:
    return FakeStore({
        ("TXFL6", "regular"): (
            _bars("2026-11-20", "09:00", 2, 300, step=1)
            + _bars("2026-11-23", "09:00", 2, 330, step=1)
            + _bars("2026-11-24", "09:00", 1, 350)
        ),
        ("TXFL6", "afterhours"): (
            _bars("2026-11-20", "15:00", 1, 310)
            + _bars("2026-11-21", "01:00", 1, 320)
        ),
    })


def test_daily_bar_combines_previous_night_with_day_session():
    source = _source(_week_store())
    klines = source.get_kline(
        "TXF", "1D", 10,
        after_time=_ts("2026-11-20", "00:00"), before_time=_ts("2026-11-25", "00:00"),
    )
    by_day = {datetime.fromtimestamp(k["time"], tz=TAIPEI).date(): k for k in klines}
    assert sorted(by_day) == [date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24)]
    friday = by_day[date(2026, 11, 20)]
    assert (friday["open"], friday["close"]) == (300, 301)
    monday = by_day[date(2026, 11, 23)]
    # Friday 15:00 night open, Saturday 01:00 high, Monday day close.
    assert monday["open"] == 310
    assert monday["high"] == 332
    assert monday["close"] == 331
    assert monday["volume"] == 4
    # Daily bars are labelled at Asia/Taipei midnight of the trading day.
    assert datetime.fromtimestamp(monday["time"], tz=TAIPEI).time() == dtime(0, 0)


def test_daily_bar_regular_session_excludes_the_night():
    source = _source(_week_store())
    klines = source.get_kline(
        "TXF@regular", "1D", 10,
        after_time=_ts("2026-11-20", "00:00"), before_time=_ts("2026-11-25", "00:00"),
    )
    monday = next(k for k in klines if datetime.fromtimestamp(k["time"], tz=TAIPEI).date() == date(2026, 11, 23))
    assert (monday["open"], monday["high"], monday["close"]) == (330, 332, 331)


def test_daily_bar_follows_the_trading_calendar_over_a_holiday():
    minutes = _week_store().rows[("TXFL6", "afterhours")]
    daily = aggregate_daily(minutes, weekdays("2026-11-23"))
    assert [datetime.fromtimestamp(k["time"], tz=TAIPEI).date() for k in daily] == [date(2026, 11, 24)]


def test_weekly_bars_group_trading_days():
    source = _source(_week_store())
    klines = source.get_kline(
        "TXF", "1W", 10,
        after_time=_ts("2026-11-16", "00:00"), before_time=_ts("2026-11-30", "00:00"),
    )
    assert [datetime.fromtimestamp(k["time"], tz=TAIPEI).date() for k in klines] == [
        date(2026, 11, 16), date(2026, 11, 23),
    ]
    assert klines[1]["open"] == 310 and klines[1]["close"] == 350


# --- current session from the sidecar -------------------------------------------------


def test_today_merges_sidecar_bars_of_the_current_contract():
    store = FakeStore({("TXFL6", "regular"): _bars("2026-12-01", "08:45", 2, 400)})
    sidecar = {
        ("TXFL6", "regular"): _bars("2026-12-01", "08:46", 3, 401),  # overlaps one stored minute
        ("TXFL6", "afterhours"): _bars("2026-11-30", "15:00", 1, 395),
    }
    source = _source(store, now="2026-12-01T08:49", sidecar=sidecar)
    klines = source.get_kline("TXF", "1m", 50)
    assert _prices(klines) == [
        ("11-30 15:00", 395), ("12-01 08:45", 400), ("12-01 08:46", 401), ("12-01 08:47", 401), ("12-01 08:48", 401),
    ]
    assert set(source.sidecar_calls) == {("TXFL6", "regular"), ("TXFL6", "afterhours")}


def test_sidecar_failure_falls_back_to_stored_bars():
    store = FakeStore({("TXFL6", "regular"): _bars("2026-12-01", "08:45", 2, 400)})
    down = FubonSidecarError("sidecar is unreachable")
    source = _source(store, now="2026-12-01T09:00", sidecar={
        ("TXFL6", "regular"): down, ("TXFL6", "afterhours"): down,
    })
    assert len(source.get_kline("TXF", "1m", 50)) == 2


def test_sidecar_failure_without_stored_bars_raises():
    down = FubonSidecarError("sidecar is unreachable")
    source = _source(FakeStore(), now="2026-12-01T09:00", sidecar={
        ("TXFL6", "regular"): down, ("TXFL6", "afterhours"): down,
    })
    with pytest.raises(FubonSidecarError, match="unreachable"):
        source.get_kline("TXF", "1m", 50)


def test_regular_session_only_asks_the_sidecar_for_the_day_session():
    source = _source(FakeStore(), now="2026-12-01T09:00", sidecar={
        ("TXFL6", "regular"): _bars("2026-12-01", "08:45", 1, 400),
    })
    assert len(source.get_kline("TXF@regular", "1m", 50)) == 1
    assert source.sidecar_calls == [("TXFL6", "regular")]


def test_invalid_symbol_and_timeframe():
    source = _source(FakeStore())
    assert source.get_kline("TXFM6", "1m", 10) == []
    with pytest.raises(Exception, match="unsupported timeframe"):
        source.get_kline("TXF", "2h", 10)


# --- earliest bar, ticker -------------------------------------------------------------


def test_earliest_bar_covers_every_contract_month_of_a_continuous_code():
    store = FakeStore()
    store.earliest = _ts("2026-11-20", "15:00")
    source = _source(store)
    assert source.get_earliest_kline_time("TXF", "1m") == _ts("2026-11-20", "15:00")
    assert store.earliest_calls[-1] == {"pattern": "TXF__", "sessions": ("afterhours", "regular")}
    # Daily: midnight of the trading day the night bar belongs to (Monday).
    assert source.get_earliest_kline_time("TXF", "1D") == _ts("2026-11-23", "00:00")
    source.get_earliest_kline_time("TXFH6@regular", "1H")
    assert store.earliest_calls[-1] == {"pattern": "TXFH6", "sessions": ("regular",)}


def test_earliest_bar_unknown_without_data():
    assert _source(FakeStore()).get_earliest_kline_time("TXF", "1D") is None


def _daily_row(day: str, open_: float, close: float, volume: float = 10.0) -> Dict[str, Any]:
    return {
        "time": _ts(day, "00:00"), "open": open_, "high": max(open_, close) + 5,
        "low": min(open_, close) - 5, "close": close, "volume": volume,
    }


def test_earliest_daily_reflects_the_taifex_daily_import():
    store = FakeStore()
    store.earliest = _ts("2026-11-20", "15:00")
    store.earliest_daily = _ts("2020-01-02", "00:00")
    source = _source(store)
    assert source.get_earliest_kline_time("TWFutures:TXF", "1D") == _ts("2020-01-02", "00:00")
    assert source.get_earliest_kline_time("TXF", "1W") == _ts("2020-01-02", "00:00")
    # Intraday timeframes still start at the first 1m bar.
    assert source.get_earliest_kline_time("TXF", "1H") == _ts("2026-11-20", "15:00")
    store.earliest = None
    assert source.get_earliest_kline_time("TXF", "1D") == _ts("2020-01-02", "00:00")
    assert source.get_earliest_kline_time("TXF", "5m") is None


def test_data_coverage_reports_the_daily_import_start(monkeypatch):
    from app.data_sources.factory import DataSourceFactory
    from app.services.data_coverage import describe_data_coverage

    store = FakeStore()
    store.earliest = _ts("2026-11-20", "15:00")
    store.earliest_daily = _ts("2020-01-02", "00:00")
    source = _source(store)
    monkeypatch.setattr(DataSourceFactory, "_resolve_source", classmethod(lambda cls, *a, **k: source))
    daily = describe_data_coverage(market="TWFutures", symbol="TXF", timeframe="1D")
    # Asia/Taipei midnight of 2020-01-02 is 16:00 UTC the day before.
    assert daily["earliest_bar"] == {"status": "known", "time": "2020-01-01T16:00:00Z", "timestamp": store.earliest_daily}
    minute = describe_data_coverage(market="TWFutures", symbol="TXF", timeframe="1m")
    assert minute["earliest_bar"]["timestamp"] == store.earliest


def test_daily_bars_fall_back_to_stored_taifex_daily_rows():
    # November 2020: TXFK0 expires on 2020-11-18 (third Wednesday).
    store = FakeStore(
        rows={("TXFL6", "regular"): _bars("2026-11-30", "08:45", 2, 500)},
        daily={
            ("TXFK0", "afterhours"): [_daily_row("2020-11-16", 100, 104, 3)],
            ("TXFK0", "regular"): [_daily_row("2020-11-16", 105, 110, 7), _daily_row("2020-11-18", 120, 121)],
            # The next month's rows before the roll are not the near month.
            ("TXFL0", "regular"): [_daily_row("2020-11-18", 900, 901), _daily_row("2020-11-19", 130, 131)],
        },
    )
    source = _source(store, now="2026-12-01T10:00")
    klines = source.get_kline(
        "TXF", "1D", 20, after_time=_ts("2020-11-16", "00:00"), before_time=_ts("2020-11-20", "00:00"),
    )
    by_day = {datetime.fromtimestamp(k["time"], tz=TAIPEI).date(): k for k in klines}
    assert sorted(by_day) == [date(2020, 11, 16), date(2020, 11, 18), date(2020, 11, 19)]
    monday = by_day[date(2020, 11, 16)]
    # Night (previous evening) open, day close, both volumes.
    assert (monday["open"], monday["close"], monday["volume"]) == (100, 110, 10)
    assert by_day[date(2020, 11, 18)]["close"] == 121
    assert by_day[date(2020, 11, 19)]["close"] == 131

    regular = source.get_kline(
        "TXF@regular", "1D", 20, after_time=_ts("2020-11-16", "00:00"), before_time=_ts("2020-11-17", "00:00"),
    )
    assert [(k["open"], k["close"]) for k in regular] == [(105, 110)]


def test_minute_derived_daily_bar_wins_over_stored_daily_row():
    store = FakeStore(
        rows={("TXFL6", "regular"): _bars("2026-11-30", "08:45", 2, 500)},
        daily={("TXFL6", "regular"): [_daily_row("2026-11-30", 1, 2), _daily_row("2026-11-27", 3, 4)]},
    )
    source = _source(store, now="2026-12-01T10:00")
    klines = source.get_kline(
        "TXF@regular", "1D", 10, after_time=_ts("2026-11-27", "00:00"), before_time=_ts("2026-12-01", "00:00"),
    )
    assert [(datetime.fromtimestamp(k["time"], tz=TAIPEI).date(), k["close"]) for k in klines] == [
        (date(2026, 11, 27), 4), (date(2026, 11, 30), 500),
    ]


def test_daily_bars_survive_a_sidecar_outage_when_stored_daily_rows_exist():
    down = FubonSidecarError("sidecar is unreachable")
    store = FakeStore(daily={("TXFL6", "regular"): [_daily_row("2026-11-30", 1, 2)]})
    source = _source(store, now="2026-12-01T09:00", sidecar={
        ("TXFL6", "regular"): down, ("TXFL6", "afterhours"): down,
    })
    assert [k["close"] for k in source.get_kline("TXF@regular", "1D", 5)] == [2]
    with pytest.raises(FubonSidecarError):
        _source(FakeStore(), now="2026-12-01T09:00", sidecar={
            ("TXFL6", "regular"): down, ("TXFL6", "afterhours"): down,
        }).get_kline("TXF", "1D", 5)


def test_ticker_quotes_the_near_month_contract(monkeypatch):
    source = _source(FakeStore(), now="2026-11-18T13:31")
    requests: List[Tuple[str, Dict[str, Any]]] = []

    def fake_request(path, params):
        requests.append((path, params))
        if path == "/v1/futopt/tickers":
            return {"tickers": [
                {"symbol": "TXFK6", "settlementDate": "2026-11-18", "endDate": "2026-11-18"},
                {"symbol": "TXFL6", "settlementDate": "2026-12-16", "endDate": "2026-12-16"},
            ]}
        return {"lastPrice": 23000, "previousClose": 22900, "name": "TXF L6"}

    monkeypatch.setattr(source, "_request", fake_request)
    ticker = source.get_ticker("TWFutures:TXF")
    assert ticker["contract"] == "TXFL6"
    assert ticker["last"] == 23000
    assert ticker["change"] == 100
    assert requests[-1] == ("/v1/futopt/quote", {"symbol": "TXFL6"})


def test_ticker_falls_back_to_the_calendar_near_month(monkeypatch):
    source = _source(FakeStore(), now="2026-11-18T13:29")

    def fake_request(path, params):
        if path == "/v1/futopt/tickers":
            raise FubonSidecarError("down")
        return {"lastPrice": 1}

    monkeypatch.setattr(source, "_request", fake_request)
    assert source.resolve_contract("TXF") == "TXFK6"
    assert source.resolve_contract("MXFH7") == "MXFH7"
