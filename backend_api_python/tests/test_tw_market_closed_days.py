"""Unscheduled Taiwan market closures (typhoon days) and the repair command (issue #86).

2026-10-23 was a typhoon day: TWSE and TAIFEX were closed, but the
``exchange_calendars`` XTAI calendar lists it as a trading day. No test touches
PostgreSQL or the TAIFEX website.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data_sources.tw_futures import trading_day_of
from app.data_sources.tw_futures_contracts import (
    BUILTIN_TW_MARKET_CLOSED_DAYS,
    TW_MARKET_CLOSED_DAYS_ENV,
    configured_closed_days,
    expiry_date,
    next_trading_day,
    tw_market_closed_days,
    xtai_is_trading_day,
)
from app.services.market_schedule import _calendar
from app.services.taifex_history import ImportReport, parse_daily_report
from app.services.tw_kline_collector import xtai_sessions
from app.services.tw_market_closure_repair import (
    ClosedDayError,
    apply_plan,
    build_plan,
    previous_trading_day,
    reimport_ranges,
)

TAIPEI = ZoneInfo("Asia/Taipei")
TYPHOON = date(2026, 10, 23)


def _ts(day: str, clock: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI).timestamp())


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv(TW_MARKET_CLOSED_DAYS_ENV, raising=False)


# --------------------------------------------------------------------------- calendar
def test_builtin_list_closes_the_typhoon_day_without_configuration(no_env):
    assert TYPHOON in BUILTIN_TW_MARKET_CLOSED_DAYS
    assert "typhoon" in BUILTIN_TW_MARKET_CLOSED_DAYS[TYPHOON]
    assert configured_closed_days() == frozenset()
    assert xtai_is_trading_day(TYPHOON) is False
    assert xtai_is_trading_day(date(2026, 10, 22)) is True
    assert next_trading_day(date(2026, 10, 22)) == date(2026, 10, 27)


def test_configured_closed_day_matches_the_issue_example(monkeypatch):
    monkeypatch.setenv(TW_MARKET_CLOSED_DAYS_ENV, "2026-10-23")
    assert xtai_is_trading_day(TYPHOON) is False
    assert next_trading_day(date(2026, 10, 22)) == date(2026, 10, 27)
    # The night session opened on Thursday 10-22 belongs to Tuesday 10-27.
    assert trading_day_of(_ts("2026-10-22", "15:00")) == date(2026, 10, 27)
    assert trading_day_of(_ts("2026-10-23", "04:59")) == date(2026, 10, 27)


def test_configured_days_add_to_the_builtin_list(monkeypatch):
    monkeypatch.setenv(TW_MARKET_CLOSED_DAYS_ENV, " 2026-11-04 ,not-a-date,; 2026-11-05")
    assert configured_closed_days() == frozenset({date(2026, 11, 4), date(2026, 11, 5)})
    assert tw_market_closed_days() >= {TYPHOON, date(2026, 11, 4), date(2026, 11, 5)}
    assert xtai_is_trading_day(date(2026, 11, 4)) is False
    assert next_trading_day(date(2026, 11, 3)) == date(2026, 11, 6)
    monkeypatch.setenv(TW_MARKET_CLOSED_DAYS_ENV, "")
    assert xtai_is_trading_day(date(2026, 11, 4)) is True


def test_contract_expiry_skips_a_configured_closure(monkeypatch):
    # The November 2026 third Wednesday is 11-18; a closure moves expiry to 11-19.
    monkeypatch.delenv(TW_MARKET_CLOSED_DAYS_ENV, raising=False)
    assert expiry_date(2026, 11) == date(2026, 11, 18)
    monkeypatch.setenv(TW_MARKET_CLOSED_DAYS_ENV, "2026-11-18")
    assert expiry_date(2026, 11) == date(2026, 11, 19)


def test_market_schedule_and_collector_calendars_skip_closures(monkeypatch):
    monkeypatch.delenv(TW_MARKET_CLOSED_DAYS_ENV, raising=False)
    for market in ("TWStock", "TWFutures"):
        assert not _calendar(market).is_session(pd.Timestamp(TYPHOON))
        assert _calendar(market).is_session(pd.Timestamp("2026-10-22"))
    sessions = xtai_sessions(date(2026, 10, 19), date(2026, 10, 30))
    assert TYPHOON not in sessions
    assert date(2026, 10, 22) in sessions and date(2026, 10, 27) in sessions

    monkeypatch.setenv(TW_MARKET_CLOSED_DAYS_ENV, "2026-10-29")
    assert date(2026, 10, 29) not in xtai_sessions(date(2026, 10, 26), date(2026, 10, 30))


def test_daily_report_after_hours_row_moves_to_the_next_open_day(no_env):
    text = "\n".join([
        "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,"
        "最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量",
        "2026/10/22,TX,202611     ,47000,47600,46900,47450,450,0.96%,90000,47450,90000,-,-,48000,18000,,一般,-",
        "2026/10/22,TX,202611     ,46991,47600,46950,47482,32,0.07%,35753,-,-,-,-,48000,18000,,盤後,-",
    ])
    bars = {bar.session: bar for bar in parse_daily_report(text)}
    assert bars["regular"].trade_date == date(2026, 10, 22)
    assert bars["afterhours"].trade_date == date(2026, 10, 27)
    assert bars["afterhours"].as_bar()["time"] == _ts("2026-10-27", "00:00")


# --------------------------------------------------------------------------- repair
class FakeRepository:
    """In-memory ``qd_tw_klines`` 1D rows and collection statuses."""

    def __init__(self) -> None:
        self.bars: Dict[Tuple[str, str, str, str, int], Dict[str, Any]] = {}
        self.statuses: Dict[Tuple[str, str, str, date], Dict[str, Any]] = {}

    def add_bar(self, symbol, session, day, close, *, volume=0.0, source="taifex"):
        ts = _ts(day, "00:00")
        self.bars[("TWFutures", symbol, session, "1D", ts)] = {
            "market": "TWFutures", "symbol": symbol, "session": session, "timeframe": "1D", "time": ts,
            "open": close, "high": close, "low": close, "close": close, "volume": volume, "source": source,
        }

    def find_bars_at(self, *, market, timeframe, bar_times):
        wanted = set(bar_times)
        return [dict(bar) for key, bar in sorted(self.bars.items())
                if key[0] == market and key[3] == timeframe and key[4] in wanted]

    def delete_bars(self, keys):
        keys = list(keys)
        for key in keys:
            self.bars.pop(key, None)
        return len(keys)

    def find_statuses(self, *, trade_dates, status):
        days = set(trade_dates)
        return [dict(row) for key, row in sorted(self.statuses.items())
                if key[3] in days and row["status"] == status]

    def mark_statuses(self, keys, *, from_status, to_status, note):
        count = 0
        for key in keys:
            row = self.statuses.get(key)
            if row and row["status"] == from_status:
                row.update(status=to_status, last_error=note)
                count += 1
        return count

    def upsert_bars(self, *, market, symbol, session, timeframe, bars, source):
        rows = list(bars)
        for bar in rows:
            key = (market, symbol, session, timeframe, int(bar["time"]))
            self.bars[key] = {"market": market, "symbol": symbol, "session": session, "timeframe": timeframe,
                              "source": source, **bar}
        return len(rows)


def _production_like_repository() -> FakeRepository:
    repo = FakeRepository()
    repo.add_bar("TXFK6", "afterhours", "2026-10-23", 47482, volume=35753)
    repo.add_bar("TXFK6", "regular", "2026-10-22", 47450, volume=90000)
    status_key = ("TWFutures", "TAIFEX_TICKS", "all", TYPHOON)
    repo.statuses[status_key] = {
        "market": "TWFutures", "symbol": "TAIFEX_TICKS", "session": "all", "trade_date": TYPHOON,
        "status": "failed", "attempts": 5, "last_error": "source=web_zip: TAIFEX header not found",
    }
    other = ("TWFutures", "TAIFEX_TICKS", "all", date(2026, 10, 22))
    repo.statuses[other] = {**repo.statuses[status_key], "trade_date": date(2026, 10, 22), "status": "success"}
    return repo


class FakeImporter:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.calls: List[Tuple[date, date, List[str]]] = []

    def import_daily(self, start, end, products):
        self.calls.append((start, end, list(products)))
        # The corrected calendar labels the 10-22 night on 10-27.
        self.repository.add_bar("TXFK6", "afterhours", "2026-10-27", 47482, volume=35753)
        report = ImportReport()
        report.add("TXFK6", "afterhours", "1D", 1)
        return report


def test_reimport_ranges_span_the_neighbouring_trading_days(no_env):
    assert previous_trading_day(TYPHOON) == date(2026, 10, 22)
    assert reimport_ranges([TYPHOON]) == [(date(2026, 10, 22), date(2026, 10, 27))]
    weekdays = lambda day: day.weekday() < 5 and day not in {date(2026, 11, 4), date(2026, 11, 5)}  # noqa: E731
    assert reimport_ranges([date(2026, 11, 5), date(2026, 11, 4)], weekdays) == [
        (date(2026, 11, 3), date(2026, 11, 6)),
    ]


def test_plan_lists_the_mislabelled_row_and_the_failed_status(no_env):
    repo = _production_like_repository()
    plan = build_plan([TYPHOON], repository=repo)
    assert [(b["symbol"], b["session"], b["close"], b["volume"]) for b in plan.bad_bars] == [
        ("TXFK6", "afterhours", 47482, 35753),
    ]
    assert plan.status_keys() == [("TWFutures", "TAIFEX_TICKS", "all", TYPHOON)]
    assert plan.reimport == [(date(2026, 10, 22), date(2026, 10, 27))]
    assert plan.products == ["TX"]


def test_plan_refuses_a_day_the_calendar_still_trades(no_env):
    with pytest.raises(ClosedDayError):
        build_plan([date(2026, 10, 22)], repository=FakeRepository())


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair_tw_market_closed_days.py"
    spec = importlib.util.spec_from_file_location("repair_tw_market_closed_days_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_dry_run_prints_the_changes_and_touches_nothing(no_env, capsys):
    script = _load_script()
    repo = _production_like_repository()
    before_bars = {key: dict(value) for key, value in repo.bars.items()}
    before_statuses = {key: dict(value) for key, value in repo.statuses.items()}

    class ExplodingImporter:
        def import_daily(self, *args, **kwargs):
            raise AssertionError("a dry run must not download")

    assert script.run(["--days", "2026-10-23"], repository=repo, importer=ExplodingImporter()) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry-run"
    plan = output["plan"]
    assert plan["closed_days"] == ["2026-10-23"]
    assert plan["delete_daily_bars"] == [{
        "market": "TWFutures", "symbol": "TXFK6", "session": "afterhours", "timeframe": "1D",
        "trade_date": "2026-10-23", "open": 47482, "high": 47482, "low": 47482, "close": 47482,
        "volume": 35753, "source": "taifex",
    }]
    assert [row["symbol"] for row in plan["mark_statuses_missed"]] == ["TAIFEX_TICKS"]
    assert "typhoon" in plan["mark_statuses_missed"][0]["note"]
    assert plan["reimport_daily"] == [{"from": "2026-10-22", "to": "2026-10-27", "products": ["TX"]}]
    assert repo.bars == before_bars
    assert repo.statuses == before_statuses


def test_cli_default_days_cover_the_builtin_list(no_env, capsys):
    script = _load_script()
    assert script.run([], repository=_production_like_repository()) == 0
    assert "2026-10-23" in json.loads(capsys.readouterr().out)["plan"]["closed_days"]


def test_cli_apply_deletes_marks_and_reimports(no_env, capsys):
    script = _load_script()
    repo = _production_like_repository()
    importer = FakeImporter(repo)
    assert script.run(["--days", "2026-10-23", "--apply", "--products", "TXF,MXF"],
                      repository=repo, importer=importer) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["deleted_daily_bars"] == 1
    assert output["result"]["marked_missed"] == 1
    assert importer.calls == [(date(2026, 10, 22), date(2026, 10, 27), ["TX", "MTX"])]
    assert ("TWFutures", "TXFK6", "afterhours", "1D", _ts("2026-10-23", "00:00")) not in repo.bars
    assert repo.bars[("TWFutures", "TXFK6", "afterhours", "1D", _ts("2026-10-27", "00:00"))]["volume"] == 35753
    assert repo.bars[("TWFutures", "TXFK6", "regular", "1D", _ts("2026-10-22", "00:00"))]["close"] == 47450
    status = repo.statuses[("TWFutures", "TAIFEX_TICKS", "all", TYPHOON)]
    assert status["status"] == "missed" and "typhoon" in status["last_error"]
    assert repo.statuses[("TWFutures", "TAIFEX_TICKS", "all", date(2026, 10, 22))]["status"] == "success"

    # Re-running finds nothing left to delete or mark; the re-import is an idempotent upsert.
    assert script.run(["--days", "2026-10-23", "--apply"], repository=repo, importer=importer) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["plan"]["delete_daily_bars"] == [] and rerun["plan"]["mark_statuses_missed"] == []


def test_cli_rejects_open_days_and_bad_dates(no_env, capsys):
    script = _load_script()
    assert script.run(["--days", "2026-10-22"], repository=FakeRepository()) == 2
    assert "still a trading day" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        script.run(["--days", "2026-13-01"], repository=FakeRepository())
    with pytest.raises(SystemExit):
        script.run(["--days", "2026-10-23", "--products", "XYZ"], repository=FakeRepository())
