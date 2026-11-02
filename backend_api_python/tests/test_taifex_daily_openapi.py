"""Daily 1D bars from the TAIFEX OpenAPI ``DailyMarketReportFut`` (issue #77).

No network and no PostgreSQL: the OpenAPI response is a fixture streamed through
the real :class:`OpenApiResult`, and the repository is an in-memory stand-in
that applies the "``source=fubon`` is never replaced" upsert rule.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest

from app.services import taifex_daily_openapi as daily
from app.services.taifex_daily_openapi import (
    STATUS_SESSION,
    STATUS_SYMBOL,
    TaifexDailyOpenApiCollector,
    expected_days,
    parse_openapi_daily_rows,
)
from app.services.taifex_history import parse_daily_report
from app.services.taifex_openapi import OpenApiResult, RawResponse, TaifexOpenApiNetworkError

TAIPEI = ZoneInfo("Asia/Taipei")


def _midnight(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime(0, 0), tzinfo=TAIPEI).timestamp())


def _now(day: str, clock: str = "08:00") -> datetime:
    return datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI)


def weekdays(*holidays: str):
    closed = {date.fromisoformat(item) for item in holidays}
    return lambda day: day.weekday() < 5 and day not in closed


def _row(day: str, contract: str, month: str, open_: str, high: str, low: str, last: str,
         volume: str, session: str) -> Dict[str, str]:
    return {
        "Date": day, "Contract": contract, "ContractMonth(Week)": month,
        "Open": open_, "High": high, "Low": low, "Last": last,
        "Change": "-", "%": "-", "Volume": volume,
        "SettlementPrice": "NULL", "OpenInterest": "-", "TradingSession": session,
    }


# 2026-10-30 is a Friday. Same content as the futDataDown fixture below.
OPENAPI_ROWS = [
    _row("20261030", "TX", "202611", "23000", "23100", "22900", "23050", "80000", "一般"),
    _row("20261030", "TX", "202611", "23040", "23080", "23010", "23060", "30000", "盤後"),
    _row("20261030", "TX", "202612", "23100", "23150", "23000", "23120", "500", "一般"),
    _row("20261030", "TX", "202611W1", "23001", "23101", "22901", "23051", "900", "一般"),
    _row("20261030", "TX", "202611/202612", "70", "75", "65", "72", "300", "一般"),
    _row("20261030", "TX", "202703", "NULL", "NULL", "NULL", "NULL", "0", "一般"),
    _row("20261030", "MTX", "202611", "23002", "23102", "22902", "23052", "120000", "一般"),
    _row("20261030", "MTX", "202612", "-", "23110", "23000", "23100", "5", "盤後"),
    _row("20261030", "MTX", "202701", "", "", "", "", "", "一般"),
    _row("20261030", "TE", "202611", "1200", "1210", "1190", "1205", "800", "一般"),
    _row("20261030", "TXO", "202611", "10", "11", "9", "10", "1", "一般"),
]

DAILY_HEADER = (
    "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,"
    "最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"
)
FUTDATADOWN_REPORT = "\n".join([
    DAILY_HEADER,
    "2026/10/30,TX,202611     ,23000,23100,22900,23050,50,0.22%,80000,23048,90000,23049,23050,24000,18000,,一般,-",
    "2026/10/30,TX,202611     ,23040,23080,23010,23060,10,0.04%,30000,-,-,23059,23060,24000,18000,,盤後,-",
    "2026/10/30,TX,202612     ,23100,23150,23000,23120,40,0.17%,500,23120,4000,-,-,24100,18100,,一般,-",
    "2026/10/30,TX,202611W1   ,23001,23101,22901,23051,50,0.22%,900,23050,100,-,-,24000,18000,,一般,-",
    "2026/10/30,TX,202611/202612,70,75,65,72,2,-,300,-,-,-,-,-,-,,一般,-",
    "2026/10/30,TX,202703     ,-,-,-,-,-,-,0,23300,10,-,-,-,-,,一般,-",
    "2026/10/30,MTX,202611     ,23002,23102,22902,23052,50,0.22%,120000,23048,50000,-,-,24000,18000,,一般,-",
    "2026/10/30,MTX,202612     ,-,23110,23000,23100,1,0.01%,5,-,-,-,-,-,-,,盤後,-",
    "2026/10/30,TE,202611     ,1200,1210,1190,1205,5,0.4%,800,1205,3000,-,-,1300,900,,一般,-",
    "2026/10/30,TXO,202611     ,10,11,9,10,0,0%,1,-,-,-,-,-,-,,一般,-",
])


def _result(rows: List[Dict[str, Any]], chunk: int = 97) -> OpenApiResult:
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)]
    return OpenApiResult(daily.ENDPOINT, "https://openapi.example/v1/" + daily.ENDPOINT,
                         RawResponse(headers={}, chunks=chunks))


class FakeClient:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None, error: Optional[Exception] = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: List[str] = []

    def fetch(self, endpoint: str) -> OpenApiResult:
        self.calls.append(endpoint)
        if self.error is not None:
            raise self.error
        return _result(self.rows)


class FakeRepository:
    """In-memory ``qd_tw_klines`` + status table with the fubon-wins upsert rule."""

    def __init__(self) -> None:
        self.bars: Dict[Tuple[str, str, str, str, int], Dict[str, Any]] = {}
        self.statuses: Dict[Tuple[str, str, str, date], str] = {}
        self.status_calls: List[Dict[str, Any]] = []
        self.fail_writes = False

    def upsert_bars(self, *, market, symbol, session, timeframe, bars, source):
        if self.fail_writes:
            raise RuntimeError("db down")
        rows = list(bars)
        for bar in rows:
            key = (market, symbol, session, timeframe, int(bar["time"]))
            existing = self.bars.get(key)
            if existing is not None and existing["source"] == "fubon" and source != "fubon":
                continue
            self.bars[key] = {**bar, "source": source}
        return len(rows)

    def latest_bar_time(self, *, market, session, timeframe, source, since=None):
        floor = int(since.timestamp()) if since is not None else None
        times = [
            ts for (m, _symbol, s, tf, ts), bar in self.bars.items()
            if (m, s, tf) == (market, session, timeframe) and bar["source"] == source
            and (floor is None or ts >= floor)
        ]
        return max(times) if times else None

    def load_statuses(self, since):
        return {key: value for key, value in self.statuses.items() if key[3] >= since}

    def record_status(self, *, market, symbol, session, trade_date, status, bar_count=0, error="", attempted=True):
        self.statuses[(market, symbol, session, trade_date)] = status
        self.status_calls.append({
            "market": market, "symbol": symbol, "session": session, "trade_date": trade_date,
            "status": status, "bar_count": bar_count, "error": error, "attempted": attempted,
        })

    def status(self, day: str) -> Optional[str]:
        return self.statuses.get(("TWFutures", STATUS_SYMBOL, STATUS_SESSION, date.fromisoformat(day)))

    def get(self, symbol, session, ts):
        return self.bars.get(("TWFutures", symbol, session, "1D", ts))


def _collector(client: FakeClient, repo: FakeRepository, **kwargs: Any) -> TaifexDailyOpenApiCollector:
    kwargs.setdefault("is_trading_day", weekdays())
    return TaifexDailyOpenApiCollector(client=client, repository=repo, **kwargs)


# --------------------------------------------------------------------------- parsing
def test_regular_and_afterhours_rows_with_weekend_night_attribution():
    bars = parse_openapi_daily_rows(_result(OPENAPI_ROWS), is_trading_day=weekdays())
    keyed = {(bar.symbol, bar.session): bar for bar in bars}
    assert set(keyed) == {
        ("TXFK6", "regular"), ("TXFK6", "afterhours"), ("TXFL6", "regular"),
        ("MXFK6", "regular"), ("EXFK6", "regular"),
    }
    day = keyed[("TXFK6", "regular")]
    assert day.trade_date == date(2026, 10, 30)
    assert (day.open, day.high, day.low, day.close, day.volume) == (23000, 23100, 22900, 23050, 80000)
    assert day.as_bar()["time"] == _midnight("2026-10-30")
    # Friday's after-hours row: the night session that belongs to Monday 11-02.
    night = keyed[("TXFK6", "afterhours")]
    assert night.trade_date == date(2026, 11, 2)
    assert night.as_bar()["time"] == _midnight("2026-11-02")
    assert (night.open, night.close, night.volume) == (23040, 23060, 30000)


def test_night_attribution_skips_a_holiday():
    bars = parse_openapi_daily_rows(OPENAPI_ROWS, products=["TXF"], is_trading_day=weekdays("2026-11-02"))
    night = next(bar for bar in bars if bar.session == "afterhours")
    assert night.trade_date == date(2026, 11, 3)
    assert {bar.symbol[:3] for bar in bars} == {"TXF"}


def test_spreads_weekly_options_and_missing_prices_are_skipped():
    bars = parse_openapi_daily_rows(OPENAPI_ROWS, is_trading_day=weekdays())
    symbols = {bar.symbol for bar in bars}
    # 202611W1 (weekly), 202611/202612 (spread), TXO (option), and NULL / "-" / blank prices.
    assert "TXFH7" not in symbols  # 202703 with NULL prices
    assert ("MXFL6", "afterhours") not in {(bar.symbol, bar.session) for bar in bars}  # Open "-"
    assert "MXFA7" not in symbols  # blank prices
    assert len(bars) == 5
    spread = _row("20261030", "TX/MTX", "202611", "1", "1", "1", "1", "1", "一般")
    assert parse_openapi_daily_rows([spread], is_trading_day=weekdays()) == []


def test_missing_volume_is_zero_and_unknown_session_is_skipped():
    rows = [
        _row("20261030", "TX", "202611", "1", "2", "0.5", "1.5", "NULL", "一般"),
        _row("20261030", "TX", "202612", "1", "2", "0.5", "1.5", "3", "其他"),
        {**_row("20261030", "TX", "202701", "1", "2", "0.5", "1.5", "3", ""), "TradingSession": ""},
        _row("bad", "TX", "202702", "1", "2", "0.5", "1.5", "3", "一般"),
    ]
    bars = parse_openapi_daily_rows(rows, is_trading_day=weekdays())
    assert [(bar.symbol, bar.session, bar.volume) for bar in bars] == [
        ("TXFK6", "regular", 0.0), ("TXFA7", "regular", 3.0),
    ]


def test_matches_parse_daily_report_for_the_same_day():
    """Same trading day from both sources -> identical bars (futDataDown fixture)."""
    check = weekdays()
    from_openapi = parse_openapi_daily_rows(_result(OPENAPI_ROWS, chunk=7), is_trading_day=check)
    from_csv = parse_daily_report(FUTDATADOWN_REPORT, is_trading_day=check)
    key = lambda bar: (bar.symbol, bar.session)  # noqa: E731
    assert sorted(from_openapi, key=key) == sorted(from_csv, key=key)
    assert [bar.as_bar() for bar in sorted(from_openapi, key=key)] == [bar.as_bar() for bar in sorted(from_csv, key=key)]
    # And with a product filter.
    assert sorted(parse_openapi_daily_rows(OPENAPI_ROWS, products=["MTX"], is_trading_day=check), key=key) == \
        sorted(parse_daily_report(FUTDATADOWN_REPORT, products=["MTX"], is_trading_day=check), key=key)


def test_afterhours_switch_keeps_the_report_date():
    bars = parse_openapi_daily_rows(
        OPENAPI_ROWS, products=["TX"], is_trading_day=weekdays(), afterhours_opens_on_report_date=False,
    )
    night = next(bar for bar in bars if bar.session == "afterhours")
    assert night.trade_date == date(2026, 10, 30)


def test_expected_days_are_trading_days_before_today():
    assert expected_days(date(2026, 11, 3), weekdays(), 3) == [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)]
    assert expected_days(date(2026, 11, 2), weekdays("2026-10-30"), 2) == [date(2026, 10, 28), date(2026, 10, 29)]


# --------------------------------------------------------------------------- collection
def test_collect_writes_default_products_and_records_success():
    repo = FakeRepository()
    client = FakeClient(OPENAPI_ROWS)
    result = _collector(client, repo, lookback=1).run(_now("2026-11-02"))

    assert client.calls == ["DailyMarketReportFut"]
    assert result["data_date"] == "2026-10-30"
    # DEFAULT_PRODUCTS = TX, MTX; TE is not collected.
    assert set(result["written"]) == {"TXFK6/regular", "TXFK6/afterhours", "TXFL6/regular", "MXFK6/regular"}
    assert repo.get("EXFK6", "regular", _midnight("2026-10-30")) is None
    regular = repo.get("TXFK6", "regular", _midnight("2026-10-30"))
    assert regular["close"] == 23050 and regular["source"] == "taifex"
    assert repo.get("TXFK6", "afterhours", _midnight("2026-11-02"))["close"] == 23060
    assert repo.status("2026-10-30") == "success"
    call = repo.status_calls[-1]
    assert (call["market"], call["symbol"], call["session"], call["bar_count"]) == (
        "TWFutures", "TAIFEX_DAILY", "all", 4,
    )
    assert result["collected"] == [{"trade_date": "2026-10-30", "bars": 4}]


def test_configured_products_limit_the_rows():
    repo = FakeRepository()
    result = _collector(FakeClient(OPENAPI_ROWS), repo, products=["TE"], lookback=1).run(_now("2026-11-02"))
    assert set(result["written"]) == {"EXFK6/regular"}


def test_rerun_is_idempotent_and_skips_the_download():
    repo = FakeRepository()
    client = FakeClient(OPENAPI_ROWS)
    _collector(client, repo, lookback=1).run(_now("2026-11-02"))
    snapshot = dict(repo.bars)

    second = _collector(client, repo, lookback=1).run(_now("2026-11-02", "09:00"))
    assert second["skipped"] == "every expected trading day already collected"
    assert client.calls == ["DailyMarketReportFut"]

    # Forcing a second write of the same report changes nothing.
    repo.statuses.clear()
    _collector(client, repo, lookback=1).run(_now("2026-11-02", "10:00"))
    assert repo.bars == snapshot


def test_never_replaces_a_fubon_bar():
    repo = FakeRepository()
    key = ("TWFutures", "TXFK6", "regular", "1D", _midnight("2026-10-30"))
    repo.bars[key] = {"time": key[-1], "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "source": "fubon"}
    _collector(FakeClient(OPENAPI_ROWS), repo, lookback=1).run(_now("2026-11-02"))
    assert repo.bars[key]["source"] == "fubon" and repo.bars[key]["close"] == 1


def test_not_published_yet_is_failed_then_collected():
    repo = FakeRepository()
    # Tuesday morning: Monday 11-02 is expected but the OpenAPI still serves Friday.
    result = _collector(FakeClient(OPENAPI_ROWS), repo, lookback=2).run(_now("2026-11-03", "06:00"))
    assert repo.status("2026-10-30") == "success"
    assert repo.status("2026-11-02") == "failed"
    assert result["failed"][0]["trade_date"] == "2026-11-02"
    assert "not published yet" in result["failed"][0]["error"]

    monday = [{**row, "Date": "20261102"} for row in OPENAPI_ROWS]
    later = _collector(FakeClient(monday), repo, lookback=2).run(_now("2026-11-03", "07:30"))
    assert repo.status("2026-11-02") == "success"
    assert later["collected"] == [{"trade_date": "2026-11-02", "bars": 4}]


def test_day_no_longer_served_is_missed_with_a_warning(caplog):
    repo = FakeRepository()
    repo.statuses[("TWFutures", STATUS_SYMBOL, STATUS_SESSION, date(2026, 10, 29))] = "failed"
    monday = [{**row, "Date": "20261102"} for row in OPENAPI_ROWS]
    with caplog.at_level("WARNING"):
        result = _collector(FakeClient(monday), repo, lookback=3).run(_now("2026-11-03"))
    assert repo.status("2026-11-02") == "success"
    assert repo.status("2026-10-29") == "missed"
    assert repo.status("2026-10-30") == "missed"
    assert [item["trade_date"] for item in result["missed"]] == ["2026-10-29", "2026-10-30"]
    missed_call = next(call for call in repo.status_calls if call["status"] == "missed")
    assert missed_call["attempted"] is False
    assert "TAIFEX daily gap" in caplog.text


def test_success_and_missed_days_are_not_revisited():
    repo = FakeRepository()
    for day, status in (("2026-10-29", "missed"), ("2026-10-30", "success")):
        repo.statuses[("TWFutures", STATUS_SYMBOL, STATUS_SESSION, date.fromisoformat(day))] = status
    client = FakeClient(OPENAPI_ROWS)
    result = _collector(client, repo, lookback=2).run(_now("2026-11-02"))
    assert result["skipped"] == "every expected trading day already collected"
    assert client.calls == []


@pytest.mark.parametrize("error", [TaifexOpenApiNetworkError("timeout"), RuntimeError("boom")])
def test_download_failure_marks_pending_days_failed(error):
    repo = FakeRepository()
    result = _collector(FakeClient(error=error), repo, lookback=2).run(_now("2026-11-03"))
    assert repo.status("2026-10-30") == "failed"
    assert repo.status("2026-11-02") == "failed"
    assert all("download failed" in item["error"] for item in result["failed"])
    assert repo.bars == {}


def test_empty_report_and_no_product_rows_are_failed():
    repo = FakeRepository()
    _collector(FakeClient([]), repo, lookback=1).run(_now("2026-11-02"))
    assert repo.status("2026-10-30") == "failed"

    repo = FakeRepository()
    te_only = [row for row in OPENAPI_ROWS if row["Contract"] == "TE"]
    result = _collector(FakeClient(te_only), repo, lookback=1).run(_now("2026-11-02"))
    assert repo.status("2026-10-30") == "failed"
    assert "no priced rows" in result["failed"][0]["error"]


def test_database_write_failure_is_failed():
    repo = FakeRepository()
    repo.fail_writes = True
    result = _collector(FakeClient(OPENAPI_ROWS), repo, lookback=1).run(_now("2026-11-02"))
    assert repo.status("2026-10-30") == "failed"
    assert "database write failed" in result["failed"][0]["error"]


def test_status_never_uses_a_contract_symbol():
    """The Fubon 1m collector keys status by contract; this collector must not collide."""
    repo = FakeRepository()
    _collector(FakeClient(OPENAPI_ROWS), repo, lookback=1).run(_now("2026-11-02"))
    assert {call["symbol"] for call in repo.status_calls} == {"TAIFEX_DAILY"}


def test_entry_point_uses_given_dependencies():
    repo = FakeRepository()
    result = daily.run_taifex_daily_collect(
        _now("2026-11-02", "07:30").astimezone(timezone.utc),
        client=FakeClient(OPENAPI_ROWS), repository=repo, is_trading_day=weekdays(), lookback=1,
    )
    assert result["collected"] == [{"trade_date": "2026-10-30", "bars": 4}]


# --------------------------------------------------------------------------- schedule (#79)
@pytest.fixture
def fubon_configured(monkeypatch):
    from app.config.data_sources import FubonSidecarConfig

    monkeypatch.setattr(type(FubonSidecarConfig), "CONFIGURED", property(lambda cls: True))
    for name in (daily.ENABLED_ENV, daily.PRODUCTS_ENV, daily.NOT_BEFORE_ENV):
        monkeypatch.delenv(name, raising=False)


def _scheduled(now: datetime, client: FakeClient, repo: FakeRepository, **kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("is_trading_day", weekdays())
    return daily.run_scheduled_daily_collect(now, client=client, repository=repo, **kwargs)


def test_daily_collect_task_is_scheduled_on_the_maintenance_queue():
    from app.celery_app import celery_app
    import app.tasks.maintenance  # noqa: F401  (registers the task)

    entry = celery_app.conf.beat_schedule["taifex-daily-collect"]
    assert entry["task"] == "quantdinger.tasks.taifex_daily_collect"
    assert entry["schedule"] >= 300
    assert celery_app.conf.task_routes["quantdinger.tasks.taifex_daily_collect"] == {"queue": "maintenance"}
    assert "quantdinger.tasks.taifex_daily_collect" in celery_app.tasks
    # The tick backfill keeps its own entry.
    assert celery_app.conf.beat_schedule["taifex-tick-backfill"]["task"] == "quantdinger.tasks.taifex_tick_backfill"


def test_scheduled_collect_skips_when_disabled_or_without_fubon(monkeypatch, fubon_configured):
    client = FakeClient(OPENAPI_ROWS)
    monkeypatch.setenv(daily.ENABLED_ENV, "false")
    assert _scheduled(_now("2026-11-02"), client, FakeRepository()) == {
        "skipped": "TAIFEX_DAILY_COLLECT_ENABLED is off",
    }

    monkeypatch.setenv(daily.ENABLED_ENV, "true")
    from app.config.data_sources import FubonSidecarConfig

    monkeypatch.setattr(type(FubonSidecarConfig), "CONFIGURED", property(lambda cls: False))
    assert _scheduled(_now("2026-11-02"), client, FakeRepository()) == {"skipped": "Fubon sidecar is not configured"}
    assert client.calls == []


def test_scheduled_collect_waits_for_the_morning_publication(monkeypatch, fubon_configured):
    client = FakeClient(OPENAPI_ROWS)
    result = _scheduled(_now("2026-11-02", "07:29"), client, FakeRepository())
    assert result == {"skipped": "before 07:30 Asia/Taipei"}
    assert client.calls == []

    monkeypatch.setenv(daily.NOT_BEFORE_ENV, "06:45")
    repo = FakeRepository()
    # A UTC instant is converted to Asia/Taipei before the gate.
    result = _scheduled(_now("2026-11-02", "06:50").astimezone(timezone.utc), client, repo, lookback=1)
    assert result["collected"] == [{"trade_date": "2026-10-30", "bars": 4}]
    assert result["freshness"] == {
        "latest": "2026-10-30", "expected": "2026-10-30", "lag_trading_days": 0, "stale": False,
    }


def test_scheduled_collect_uses_the_configured_products(monkeypatch, fubon_configured):
    monkeypatch.setenv(daily.PRODUCTS_ENV, "TXF")
    repo = FakeRepository()
    result = _scheduled(_now("2026-11-02"), FakeClient(OPENAPI_ROWS), repo, lookback=1)
    assert set(result["written"]) == {"TXFK6/regular", "TXFK6/afterhours", "TXFL6/regular"}


def test_freshness_warns_when_the_openapi_is_behind(fubon_configured):
    repo = FakeRepository()
    # Wednesday 11-04: Monday and Tuesday are due, but the OpenAPI still serves Friday 10-30.
    result = _scheduled(_now("2026-11-04", "09:00"), FakeClient(OPENAPI_ROWS), repo, lookback=3)
    assert result["freshness"] == {
        "latest": "2026-10-30", "expected": "2026-11-03", "lag_trading_days": 2, "stale": True,
    }


def test_freshness_ignores_the_afterhours_label_of_the_next_trading_day():
    """A 盤後 row of Friday is labelled Monday; only day-session bars count."""
    repo = FakeRepository()
    _collector(FakeClient(OPENAPI_ROWS), repo, lookback=1).run(_now("2026-11-02"))
    assert repo.get("TXFK6", "afterhours", _midnight("2026-11-02")) is not None
    fresh = daily.check_freshness(repo, _now("2026-11-03"), is_trading_day=weekdays())
    assert fresh["latest"] == "2026-10-30"
    assert fresh["stale"] is True and fresh["lag_trading_days"] == 1


def test_freshness_with_no_stored_bars_counts_the_scan_window():
    fresh = daily.check_freshness(FakeRepository(), _now("2026-11-03"), is_trading_day=weekdays())
    assert fresh["latest"] is None and fresh["expected"] == "2026-11-02"
    assert fresh["stale"] is True and fresh["lag_trading_days"] > 20


def test_freshness_is_not_rechecked_when_every_day_is_collected(fubon_configured):
    repo = FakeRepository()
    repo.statuses[("TWFutures", STATUS_SYMBOL, STATUS_SESSION, date(2026, 10, 30))] = "success"
    client = FakeClient(OPENAPI_ROWS)
    result = _scheduled(_now("2026-11-02"), client, repo, lookback=1)
    assert result["skipped"] == "every expected trading day already collected"
    assert "freshness" not in result and client.calls == []


def test_freshness_failure_does_not_fail_the_collection(fubon_configured):
    class BrokenFreshness(FakeRepository):
        def latest_bar_time(self, **kwargs):
            raise RuntimeError("db down")

    result = _scheduled(_now("2026-11-02"), FakeClient(OPENAPI_ROWS), BrokenFreshness(), lookback=1)
    assert result["collected"] == [{"trade_date": "2026-10-30", "bars": 4}]
    assert result["freshness"] == {"error": "db down"}


def test_configured_not_before_and_products():
    assert daily.configured_not_before("") == dtime(7, 30)
    assert daily.configured_not_before("08:15") == dtime(8, 15)
    assert daily.configured_not_before("late") == dtime(7, 30)
    assert daily.configured_products("TXF,MTX") == ["TX", "MTX"]
