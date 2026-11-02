"""Daily Taiwan 1m K-line accumulation (issue #56).

The Fubon sidecar and PostgreSQL are replaced by in-memory fakes; the trading
calendar is injected so every case is deterministic.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest

from app.data_sources.tw_stock import FubonSidecarError
from app.services import tw_kline_collector as collector_module
from app.services.tw_kline_collector import (
    DEFAULT_COLLECT_SYMBOLS,
    CollectTargets,
    TWKlineCollector,
    configured_symbol_entries,
    near_month_contracts,
    parse_collect_symbols,
    run_tw_kline_collection,
    session_windows,
    xtai_sessions,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _tp(day: str, clock: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI)


def _ts(day: str, clock: str) -> int:
    return int(_tp(day, clock).timestamp())


def _bars(day: str, clock: str, count: int, price: float = 100.0) -> List[Dict[str, Any]]:
    start = _ts(day, clock)
    return [
        {"time": start + 60 * i, "open": price, "high": price + 1, "low": price - 1, "close": price + i,
         "volume": 10.0}
        for i in range(count)
    ]


def weekday_calendar(first: str, holidays: Tuple[str, ...] = ()):
    """Trading days are weekdays from ``first`` on, minus ``holidays``."""
    first_day = date.fromisoformat(first)
    closed = {date.fromisoformat(d) for d in holidays}

    def lookup(start: date, end: date) -> List[date]:
        day = max(start, first_day)
        out = []
        while day <= end:
            if day.weekday() < 5 and day not in closed:
                out.append(day)
            day += timedelta(days=1)
        return out

    return lookup


class FakeRepository:
    def __init__(self, watchlist: Optional[List[Tuple[str, str]]] = None) -> None:
        self.bars: Dict[tuple, Dict[str, Any]] = {}
        self.statuses: Dict[tuple, Dict[str, Any]] = {}
        self.watchlist = watchlist or []
        self.upserts: List[Dict[str, Any]] = []

    def upsert_bars(self, *, market, symbol, session, timeframe, bars, source):
        bars = list(bars)
        self.upserts.append({"market": market, "symbol": symbol, "session": session,
                             "timeframe": timeframe, "count": len(bars), "source": source})
        for bar in bars:
            key = (market, symbol, session, timeframe, int(bar["time"]))
            current = self.bars.get(key)
            if current and current["source"] == "fubon" and source != "fubon":
                continue
            self.bars[key] = dict(bar, source=source)
        return len(bars)

    def load_statuses(self, since):
        return {key: row["status"] for key, row in self.statuses.items() if key[3] >= since}

    def record_status(self, *, market, symbol, session, trade_date, status, bar_count=0, error="",
                      attempted=True):
        key = (market, symbol, session, trade_date)
        row = self.statuses.setdefault(key, {"attempts": 0})
        row.update(status=status, bar_count=bar_count, error=error)
        row["attempts"] += 1 if attempted else 0

    def watchlist_symbols(self):
        return list(self.watchlist)

    def stored(self, market, symbol, session) -> List[int]:
        return sorted(k[4] for k in self.bars if k[:3] == (market, symbol, session))


class FakeSource:
    def __init__(self) -> None:
        self.stock: Dict[str, Any] = {}
        self.futures: Dict[Tuple[str, str], Any] = {}
        self.tickers: Any = []
        self.calls: List[tuple] = []

    @staticmethod
    def _result(value):
        if isinstance(value, Exception):
            raise value
        return [dict(row) for row in (value or [])]

    def fetch_minute_bars(self, code, *, include_intraday=True):
        self.calls.append(("stock", code))
        return self._result(self.stock.get(code))

    def fetch_futopt_minute_bars(self, contract, session):
        self.calls.append(("futures", contract, session))
        return self._result(self.futures.get((contract, session)))

    def get_futopt_tickers(self, ticker_type="FUTURE"):
        self.calls.append(("tickers", ticker_type))
        if isinstance(self.tickers, Exception):
            raise self.tickers
        return list(self.tickers)


TICKERS = [
    {"symbol": "TXFK6", "settlementDate": "2026-11-18"},
    {"symbol": "TXFL6", "settlementDate": "2026-12-16"},
    {"symbol": "TXFA7", "settlementDate": "2027-01-20"},
    {"symbol": "TXFJ6", "settlementDate": "2026-10-21"},  # already settled
    {"symbol": "TXFK6/L6", "settlementDate": "2026-11-18"},  # spread
    {"symbol": "MXFK6", "settlementDate": "2026-11-18"},
    {"symbol": "MXFL6", "endDate": "2026-12-16"},
    {"symbol": "TXO18000K6", "settlementDate": "2026-11-18"},  # option series
]

STOCK_ONLY = CollectTargets(stock_codes=["2330"])
TXF_ONLY = CollectTargets(futures_products=["TXF"])


def _collector(first="2026-10-28", holidays=()):
    source, repo = FakeSource(), FakeRepository()
    return TWKlineCollector(
        source=source, repository=repo, sessions_lookup=weekday_calendar(first, holidays),
    ), source, repo


# --- stocks ---------------------------------------------------------------------------


def test_stock_day_is_collected_after_1400():
    collector, source, repo = _collector()
    source.stock["2330"] = _bars("2026-10-28", "09:00", 5) + _bars("2026-10-28", "13:30", 1)

    result = collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)

    assert result["collected"] == [
        {"market": "TWStock", "symbol": "2330", "session": "regular", "trade_date": "2026-10-28", "bars": 6},
    ]
    assert len(repo.stored("TWStock", "2330", "regular")) == 6
    assert repo.upserts[0]["source"] == "fubon" and repo.upserts[0]["timeframe"] == "1m"
    assert repo.statuses[("TWStock", "2330", "regular", date(2026, 10, 28))]["status"] == "success"


def test_stock_day_is_not_fetched_before_1400():
    collector, source, _ = _collector()

    result = collector.run(_tp("2026-10-28", "13:55"), STOCK_ONLY)

    assert result == {"skipped": "no trading session ready"}
    assert source.calls == []


def test_non_trading_day_is_skipped():
    # 2026-10-29 is a holiday in this calendar; 10-28 was already collected.
    collector, source, repo = _collector(holidays=("2026-10-29",))
    source.stock["2330"] = _bars("2026-10-28", "09:00", 3)
    collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)
    source.calls.clear()

    for clock in ("14:05", "18:00"):
        result = collector.run(_tp("2026-10-29", clock), STOCK_ONLY)
        assert result["collected"] == [] and result["failed"] == []
    assert source.calls == []
    assert not any(key[3] == date(2026, 10, 29) for key in repo.statuses)


def test_weekend_is_skipped_for_stocks():
    collector, source, _ = _collector(first="2026-10-30")
    source.stock["2330"] = _bars("2026-10-30", "09:00", 3)
    collector.run(_tp("2026-10-30", "14:05"), STOCK_ONLY)
    source.calls.clear()

    result = collector.run(_tp("2026-10-31", "14:05"), STOCK_ONLY)

    assert source.calls == []
    assert result["collected"] == []


def test_rerun_is_idempotent():
    collector, source, repo = _collector()
    source.stock["2330"] = _bars("2026-10-28", "09:00", 4)
    collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)
    stored = repo.stored("TWStock", "2330", "regular")

    # A later tick finds the day done and does not fetch again.
    source.calls.clear()
    assert collector.run(_tp("2026-10-28", "14:15"), STOCK_ONLY)["collected"] == []
    assert source.calls == []

    # Re-collecting the same day (status cleared) rewrites the same bars only.
    repo.statuses.clear()
    collector.run(_tp("2026-10-28", "14:25"), STOCK_ONLY)
    assert repo.stored("TWStock", "2330", "regular") == stored


def test_failed_day_is_retried_by_the_next_run():
    collector, source, repo = _collector()
    source.stock["2330"] = FubonSidecarError("Fubon market data sidecar is not ready (503)")

    first = collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)

    key = ("TWStock", "2330", "regular", date(2026, 10, 28))
    assert first["failed"][0]["error"].startswith("Fubon market data sidecar is not ready")
    assert repo.statuses[key]["status"] == "failed"

    source.stock["2330"] = _bars("2026-10-28", "09:00", 3)
    second = collector.run(_tp("2026-10-28", "14:15"), STOCK_ONLY)

    assert [entry["trade_date"] for entry in second["collected"]] == ["2026-10-28"]
    assert repo.statuses[key]["status"] == "success"
    assert repo.statuses[key]["attempts"] == 2


def test_failed_day_is_backfilled_from_the_five_day_history():
    collector, source, repo = _collector()
    source.stock["2330"] = FubonSidecarError("Fubon market data sidecar is unreachable")
    collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)

    # Next trading day: one fetch returns both days and fills both.
    source.stock["2330"] = _bars("2026-10-28", "09:00", 3) + _bars("2026-10-29", "09:00", 2)
    result = collector.run(_tp("2026-10-29", "14:05"), STOCK_ONLY)

    assert sorted((e["trade_date"], e["bars"]) for e in result["collected"]) == [
        ("2026-10-28", 3), ("2026-10-29", 2),
    ]
    assert source.calls.count(("stock", "2330")) == 2


def test_empty_day_stays_pending():
    collector, source, repo = _collector()
    source.stock["2330"] = _bars("2026-10-27", "09:00", 3)  # yesterday only

    result = collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)

    assert result["failed"][0]["error"] == "Fubon returned no 1m bars for this trading day"
    assert repo.statuses[("TWStock", "2330", "regular", date(2026, 10, 28))]["status"] == "failed"


def test_expired_day_is_recorded_as_a_gap(caplog):
    collector, source, repo = _collector()
    source.stock["2330"] = FubonSidecarError("Fubon market data sidecar is unreachable")
    collector.run(_tp("2026-10-28", "14:05"), STOCK_ONLY)

    # Three trading days later the 5-day history no longer reliably holds 10-28.
    source.stock["2330"] = _bars("2026-11-02", "09:00", 2)
    with caplog.at_level(logging.WARNING):
        result = collector.run(_tp("2026-11-02", "14:05"), STOCK_ONLY)

    missed = [e for e in result["missed"] if e["trade_date"] == "2026-10-28"]
    assert missed and missed[0]["symbol"] == "2330"
    key = ("TWStock", "2330", "regular", date(2026, 10, 28))
    assert repo.statuses[key]["status"] == "missed"
    assert "TW K-line gap" in caplog.text

    # A gap is reported once, not on every tick.
    again = collector.run(_tp("2026-11-02", "14:15"), STOCK_ONLY)
    assert not any(e["trade_date"] == "2026-10-28" for e in again["missed"])


# --- futures --------------------------------------------------------------------------


def test_futures_day_session_collects_near_and_next_month():
    collector, source, repo = _collector()
    source.tickers = TICKERS
    day_bars = _bars("2026-10-28", "08:45", 5)
    stray = _bars("2026-10-27", "15:00", 2)  # previous night, not part of this session
    source.futures[("TXFK6", "regular")] = day_bars + stray
    source.futures[("TXFL6", "regular")] = _bars("2026-10-28", "08:45", 3)

    result = collector.run(_tp("2026-10-28", "13:55"), TXF_ONLY)

    assert sorted((e["symbol"], e["session"], e["bars"]) for e in result["collected"]) == [
        ("TXFK6", "regular", 5), ("TXFL6", "regular", 3),
    ]
    assert repo.stored("TWFutures", "TXFK6", "regular") == [b["time"] for b in day_bars]
    assert ("futures", "TXFA7", "regular") not in source.calls


def test_futures_day_session_waits_until_1350():
    collector, source, _ = _collector()
    source.tickers = TICKERS

    assert collector.run(_tp("2026-10-28", "13:45"), TXF_ONLY) == {"skipped": "no trading session ready"}
    assert source.calls == []


def test_futures_night_session_is_collected_next_morning():
    collector, source, repo = _collector()
    source.tickers = TICKERS
    for contract in ("TXFK6", "TXFL6"):
        source.futures[(contract, "regular")] = _bars("2026-10-28", "08:45", 2)
    night = _bars("2026-10-28", "15:00", 3) + _bars("2026-10-29", "04:58", 2)
    source.futures[("TXFK6", "afterhours")] = night
    source.futures[("TXFL6", "afterhours")] = _bars("2026-10-28", "15:00", 1)
    collector.run(_tp("2026-10-28", "13:55"), TXF_ONLY)

    # Not before 05:10 the next morning.
    source.calls.clear()
    assert collector.run(_tp("2026-10-29", "05:05"), TXF_ONLY)["collected"] == []
    assert not any(call[0] == "futures" for call in source.calls)

    result = collector.run(_tp("2026-10-29", "05:15"), TXF_ONLY)

    assert sorted((e["symbol"], e["session"], e["trade_date"], e["bars"]) for e in result["collected"]) == [
        ("TXFK6", "afterhours", "2026-10-28", 5), ("TXFL6", "afterhours", "2026-10-28", 1),
    ]
    assert repo.stored("TWFutures", "TXFK6", "afterhours") == [b["time"] for b in night]
    assert repo.stored("TWFutures", "TXFK6", "regular") == [b["time"] for b in _bars("2026-10-28", "08:45", 2)]


def test_friday_night_session_is_retried_until_monday():
    collector, source, repo = _collector(first="2026-10-30")
    source.tickers = TICKERS
    for contract in ("TXFK6", "TXFL6"):
        source.futures[(contract, "regular")] = _bars("2026-10-30", "08:45", 1)
        source.futures[(contract, "afterhours")] = FubonSidecarError("Fubon market data sidecar is unreachable")
    collector.run(_tp("2026-10-31", "05:15"), TXF_ONLY)
    key = ("TWFutures", "TXFK6", "afterhours", date(2026, 10, 30))
    assert repo.statuses[key]["status"] == "failed"

    for contract in ("TXFK6", "TXFL6"):
        source.futures[(contract, "afterhours")] = _bars("2026-10-30", "15:00", 2)
    collector.run(_tp("2026-11-01", "12:00"), TXF_ONLY)  # Sunday: still inside the window

    assert repo.statuses[key]["status"] == "success"


def test_futures_session_missed_after_next_morning(caplog):
    collector, source, repo = _collector()
    source.tickers = TICKERS
    source.futures[("TXFK6", "regular")] = FubonSidecarError("Fubon market data sidecar is unreachable")
    source.futures[("TXFL6", "regular")] = _bars("2026-10-28", "08:45", 1)
    collector.run(_tp("2026-10-28", "13:55"), TXF_ONLY)

    with caplog.at_level(logging.WARNING):
        result = collector.run(_tp("2026-10-29", "08:35"), TXF_ONLY)

    gaps = {(e["symbol"], e["session"]) for e in result["missed"] if e["trade_date"] == "2026-10-28"}
    assert ("TXFK6", "regular") in gaps
    assert repo.statuses[("TWFutures", "TXFK6", "regular", date(2026, 10, 28))]["status"] == "missed"
    assert "TW K-line gap" in caplog.text


def test_futures_tickers_failure_is_recorded_for_the_product():
    collector, source, repo = _collector()
    source.tickers = FubonSidecarError("Fubon market data sidecar is not ready (503)")

    result = collector.run(_tp("2026-10-28", "13:55"), TXF_ONLY)

    assert result["failed"][0]["symbol"] == "TXF"
    assert "futures tickers unavailable" in result["failed"][0]["error"]
    assert repo.statuses[("TWFutures", "TXF", "regular", date(2026, 10, 28))]["status"] == "failed"


def test_explicit_contract_is_collected_without_tickers():
    collector, source, _ = _collector()
    source.futures[("TXFL6", "regular")] = _bars("2026-10-28", "08:45", 2)

    result = collector.run(_tp("2026-10-28", "13:55"), CollectTargets(futures_contracts=["TXFL6"]))

    assert [e["symbol"] for e in result["collected"]] == ["TXFL6"]
    assert ("tickers", "FUTURE") not in source.calls


# --- near month -----------------------------------------------------------------------


def test_near_month_contracts_order_by_settlement():
    assert near_month_contracts(TICKERS, "TXF", date(2026, 10, 28)) == ["TXFK6", "TXFL6"]
    assert near_month_contracts(TICKERS, "MXF", date(2026, 10, 28)) == ["MXFK6", "MXFL6"]
    assert near_month_contracts(TICKERS, "TXF", date(2026, 10, 28), count=3) == ["TXFK6", "TXFL6", "TXFA7"]


def test_near_month_rolls_after_the_settlement_day_session():
    # Settlement day: the day session still trades the expiring contract ...
    assert near_month_contracts(TICKERS, "TXF", date(2026, 11, 18)) == ["TXFK6", "TXFL6"]
    # ... while that evening's night session trades the next two months.
    windows = session_windows(_tp("2026-11-19", "05:15"), sessions_lookup=weekday_calendar("2026-11-18"),
                              include_stock=False)
    night = next(w for w in windows if w.session == "afterhours")
    assert near_month_contracts(TICKERS, "TXF", night.contracts_as_of()) == ["TXFL6", "TXFA7"]


def test_near_month_ignores_spreads_options_and_missing_dates():
    tickers = TICKERS + [{"symbol": "TXFB7"}, {"symbol": "TXFC7", "settlementDate": "bad"}]
    assert near_month_contracts(tickers, "TXF", date(2026, 12, 17)) == ["TXFA7"]
    assert near_month_contracts(tickers, "TXO", date(2026, 10, 28)) == []


# --- configuration ----------------------------------------------------------------------


def test_default_collect_symbols(monkeypatch):
    monkeypatch.delenv("TW_KLINE_COLLECT_SYMBOLS", raising=False)
    targets = parse_collect_symbols(configured_symbol_entries())
    assert DEFAULT_COLLECT_SYMBOLS.startswith("TWStock:IX0001")
    assert targets.stock_codes == ["IX0001", "2330", "0050"]
    assert targets.futures_products == ["TXF", "MXF"]
    assert targets.futures_contracts == []


def test_configured_symbols_and_watchlist_are_merged(monkeypatch):
    monkeypatch.setenv("TW_KLINE_COLLECT_SYMBOLS", "TWStock:2330.TW, 6488.TWO ,TWFutures:TXFL6,bad:XX, ")
    repo = FakeRepository(watchlist=[("TWStock", "2330.TW"), ("TWStock", "2454.TW"), ("TWFutures", "MXF")])
    collector = TWKlineCollector(source=FakeSource(), repository=repo,
                                 sessions_lookup=weekday_calendar("2026-10-28"))

    targets = collector.resolve_targets()

    assert targets.stock_codes == ["2330", "6488", "2454"]
    assert targets.futures_products == ["MXF"]
    assert targets.futures_contracts == ["TXFL6"]


def test_watchlist_failure_keeps_configured_symbols(monkeypatch):
    monkeypatch.setenv("TW_KLINE_COLLECT_SYMBOLS", "TWStock:0050.TW")

    class BrokenRepo(FakeRepository):
        def watchlist_symbols(self):
            raise RuntimeError("db down")

    collector = TWKlineCollector(source=FakeSource(), repository=BrokenRepo(),
                                 sessions_lookup=weekday_calendar("2026-10-28"))
    assert collector.resolve_targets().stock_codes == ["0050"]


def test_no_symbols_is_skipped():
    collector, source, _ = _collector()
    assert collector.run(_tp("2026-10-28", "14:05"), CollectTargets()) == {"skipped": "no symbols configured"}
    assert source.calls == []


def test_entry_point_skips_without_sidecar_or_when_disabled(monkeypatch):
    from app.config import data_sources as data_source_config

    meta = data_source_config.MetaFubonSidecarConfig
    monkeypatch.setattr(meta, "TOKEN", property(lambda cls: ""))
    assert run_tw_kline_collection() == {"skipped": "Fubon sidecar is not configured"}

    monkeypatch.setenv("TW_KLINE_COLLECT_ENABLED", "false")
    assert run_tw_kline_collection() == {"skipped": "TW_KLINE_COLLECT_ENABLED is off"}


def test_lookback_limits_old_days(monkeypatch):
    monkeypatch.setattr(collector_module, "LOOKBACK_DAYS", 2)
    windows = session_windows(_tp("2026-11-02", "14:05"), sessions_lookup=weekday_calendar("2026-10-01"),
                              include_futures=False)
    assert sorted({w.trade_date for w in windows}) == [date(2026, 11, 2)]


# --- scheduling ---------------------------------------------------------------------------


def test_xtai_calendar_excludes_weekends_and_holidays():
    sessions = xtai_sessions(date(2025, 10, 6), date(2025, 10, 12))
    assert date(2025, 10, 10) not in sessions  # National Day
    assert all(day.weekday() < 5 for day in sessions)
    assert date(2025, 10, 7) in sessions


def test_beat_schedules_the_collection_on_the_maintenance_queue():
    from app.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["tw-kline-collect"]
    assert entry["task"] == "quantdinger.tasks.tw_kline_collect"
    assert 300 <= entry["schedule"] <= 3600
    assert celery_app.conf.task_routes["quantdinger.tasks.tw_kline_collect"] == {"queue": "maintenance"}


@pytest.mark.parametrize("now, expected", [
    (_tp("2026-10-28", "14:05"), True),
    (_tp("2026-10-28", "13:59"), False),
])
def test_stock_ready_time(now, expected):
    windows = session_windows(now, sessions_lookup=weekday_calendar("2026-10-28"), include_futures=False)
    assert bool(windows) is expected
