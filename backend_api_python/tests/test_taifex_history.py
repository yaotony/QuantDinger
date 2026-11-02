"""TAIFEX history import: parsers, product mapping, idempotent upserts, backfill (issue #58).

No test touches the TAIFEX website or PostgreSQL: the downloader is an injected
fake and the repository is an in-memory stand-in that applies the same
"``source=fubon`` is never replaced" rule as ``TWKlineRepository.upsert_bars``.
"""
from __future__ import annotations

import importlib.util
import io
import zipfile
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import pytest

from app.services import taifex_tick_backfill
from app.services.taifex_history import (
    DAILY_URL,
    TAIFEX_TO_FUBON,
    TaifexFormatError,
    TaifexHistoryImporter,
    contract_symbol,
    decode_taifex_bytes,
    month_chunks,
    parse_daily_report,
    parse_products,
    parse_tick_file,
    tick_file_url,
    ticks_to_minute_bars,
    to_fubon_product,
    to_taifex_product,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _ts(day: str, clock: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI).timestamp())


def weekdays(*holidays: str):
    closed = {date.fromisoformat(item) for item in holidays}
    return lambda day: day.weekday() < 5 and day not in closed


# --------------------------------------------------------------------------- fixtures
DAILY_HEADER = (
    "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,"
    "最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"
)
# 2026-10-30 is a Friday: its after-hours row is the night that belongs to Monday 11-02.
DAILY_REPORT = "\n".join([
    DAILY_HEADER,
    "2026/10/30,TX,202611     ,23000,23100,22900,23050,50,0.22%,80000,23048,90000,23049,23050,24000,18000,,一般,-",
    "2026/10/30,TX,202611     ,23040,23080,23010,23060,10,0.04%,30000,-,-,23059,23060,24000,18000,,盤後,-",
    "2026/10/30,TX,202612     ,23100,23150,23000,23120,40,0.17%,500,23120,4000,-,-,24100,18100,,一般,-",
    "2026/10/30,TX,202611W1   ,23001,23101,22901,23051,50,0.22%,900,23050,100,-,-,24000,18000,,一般,-",
    "2026/10/30,TX,202611/202612,70,75,65,72,2,-,300,-,-,-,-,-,-,,一般,-",
    "2026/10/30,TX,202703     ,-,-,-,-,-,-,0,23300,10,-,-,-,-,,一般,-",
    "2026/10/30,MTX,202611     ,23002,23102,22902,23052,50,0.22%,120000,23048,50000,-,-,24000,18000,,一般,-",
    "2026/10/30,TE,202611     ,1200,1210,1190,1205,5,0.4%,800,1205,3000,-,-,1300,900,,一般,-",
    "2026/10/30,TXO,202611     ,10,11,9,10,0,0%,1,-,-,-,-,-,-,,一般,-",
])

TICK_HEADER = "成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),近月價格,遠月價格,開盤集合競價 "
TICK_FILE = "\n".join([
    TICK_HEADER,
    "20261029,MTX     ,202611     ,150000,22980,4,-,-,*",
    "20261030,MTX     ,202611     ,010059,22990,2,-,-,",
    "20261030,TX      ,202611     ,084500,23000,10,-,-,*",
    "20261030,TX      ,202611     ,084512,23010,4,-,-,",
    "20261030,TX      ,202611     ,084559,22990,2,-,-,",
    "20261030,TX      ,202611     ,84600,23005,6,-,-,",
    "20261030,TX      ,202611W1   ,084510,23500,8,-,-,",
    "20261030,TX      ,202611/202612,084520,60,8,23000,23060,",
    "20261030,TX      ,202612     ,084530,23100,2,-,-,",
    "20261030,TX      ,202611     ,140500,23000,2,-,-,",
    "20261030,TXO     ,202611     ,084500,100,2,-,-,",
])


class FakeRepository:
    """In-memory ``qd_tw_klines`` + status table with the fubon-wins upsert rule."""

    def __init__(self) -> None:
        self.bars: Dict[Tuple[str, str, str, str, int], Dict[str, Any]] = {}
        self.statuses: Dict[Tuple[str, str, str, date], str] = {}
        self.status_calls: List[Dict[str, Any]] = []

    def upsert_bars(self, *, market, symbol, session, timeframe, bars, source):
        rows = list(bars)
        for bar in rows:
            key = (market, symbol, session, timeframe, int(bar["time"]))
            existing = self.bars.get(key)
            if existing is not None and existing["source"] == "fubon" and source != "fubon":
                continue
            self.bars[key] = {**bar, "source": source}
        return len(rows)

    def load_statuses(self, since):
        return {key: value for key, value in self.statuses.items() if key[3] >= since}

    def record_status(self, *, market, symbol, session, trade_date, status, bar_count=0, error="", attempted=True):
        self.statuses[(market, symbol, session, trade_date)] = status
        self.status_calls.append({"trade_date": trade_date, "status": status, "bar_count": bar_count})

    def get(self, symbol, session, timeframe, ts):
        return self.bars.get(("TWFutures", symbol, session, timeframe, ts))


# --------------------------------------------------------------------------- products
def test_product_mapping_table():
    assert TAIFEX_TO_FUBON["TX"] == "TXF"
    assert TAIFEX_TO_FUBON["MTX"] == "MXF"
    assert TAIFEX_TO_FUBON["TMF"] == "TMF"
    assert to_fubon_product("tx") == "TXF"
    assert to_fubon_product("TXO") is None
    assert to_taifex_product("MXF") == "MTX"
    assert to_taifex_product("MTX") == "MTX"
    assert parse_products("TX, MXF,TX,TMF") == ["TX", "MTX", "TMF"]
    with pytest.raises(ValueError):
        parse_products("TX,XYZ")
    # Every mapped product has TAIFEX contract specs in the TWFutures market.
    from app.data_sources.tw_futures_contracts import CONTRACT_SPECS

    assert set(TAIFEX_TO_FUBON.values()) <= set(CONTRACT_SPECS)


def test_contract_symbol_skips_weekly_and_spreads():
    assert contract_symbol("TX", "202608") == "TXFH6"
    assert contract_symbol("MTX", " 202012 ") == "MXFL0"
    assert contract_symbol("TX", "202605W4") is None
    assert contract_symbol("TX", "202605/202606") is None
    assert contract_symbol("TX/MTX", "202605") is None
    assert contract_symbol("TXO", "202605") is None
    assert contract_symbol("TX", "202613") is None


# --------------------------------------------------------------------------- decoding
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "big5", "cp950"])
def test_decode_big5_and_utf8(encoding):
    payload = DAILY_REPORT.encode(encoding)
    text = decode_taifex_bytes(payload)
    assert text.startswith("交易日期")
    assert len(parse_daily_report(text, is_trading_day=weekdays())) == 5


def test_decode_zip_archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Daily_2026_10_30.csv", TICK_FILE.encode("big5"))
    assert decode_taifex_bytes(buffer.getvalue()).startswith("成交日期")


# --------------------------------------------------------------------------- daily report
def test_daily_report_rows_and_session_attribution():
    bars = parse_daily_report(DAILY_REPORT, is_trading_day=weekdays())
    keyed = {(bar.symbol, bar.session): bar for bar in bars}
    assert set(keyed) == {
        ("TXFK6", "regular"), ("TXFK6", "afterhours"), ("TXFL6", "regular"),
        ("MXFK6", "regular"), ("EXFK6", "regular"),
    }
    day = keyed[("TXFK6", "regular")]
    assert day.trade_date == date(2026, 10, 30)
    assert (day.open, day.high, day.low, day.close, day.volume) == (23000, 23100, 22900, 23050, 80000)
    assert day.as_bar()["time"] == _ts("2026-10-30", "00:00")
    # Friday's after-hours session belongs to Monday's trading day.
    night = keyed[("TXFK6", "afterhours")]
    assert night.trade_date == date(2026, 11, 2)
    assert night.as_bar()["time"] == _ts("2026-11-02", "00:00")


def test_daily_report_night_skips_a_holiday():
    bars = parse_daily_report(DAILY_REPORT, products=["TX"], is_trading_day=weekdays("2026-11-02"))
    night = next(bar for bar in bars if bar.session == "afterhours")
    assert night.trade_date == date(2026, 11, 3)
    assert {bar.symbol[:3] for bar in bars} == {"TXF"}


def test_daily_report_without_session_column_is_regular():
    text = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量\n" \
           "2010/01/04,TX,201001,8200,8250,8150,8220,20,0.2%,90000\n"
    bars = parse_daily_report(text, is_trading_day=weekdays())
    assert [(bar.symbol, bar.session, bar.trade_date) for bar in bars] == [("TXFA0", "regular", date(2010, 1, 4))]


def test_daily_report_no_data_and_bad_format():
    assert parse_daily_report("<html><body>查無資料</body></html>") == []
    with pytest.raises(TaifexFormatError):
        parse_daily_report("<html><body>Service unavailable</body></html>")


# --------------------------------------------------------------------------- tick file
def test_tick_file_halves_quantity_and_skips_weekly_and_spreads():
    ticks = parse_tick_file(TICK_FILE)
    symbols = {tick.symbol for tick in ticks}
    assert symbols == {"TXFK6", "TXFL6", "MXFK6"}
    first = next(tick for tick in ticks if tick.symbol == "TXFK6")
    assert first.ts == _ts("2026-10-30", "08:45")
    assert first.contracts == 5  # B+S 10 -> 5 contracts
    assert all(tick.price != 23500 for tick in ticks)  # weekly contract excluded
    assert parse_tick_file(TICK_FILE, products=["MXF"])[0].symbol == "MXFK6"


def test_ticks_aggregate_to_minute_bars_by_session():
    minutes = ticks_to_minute_bars(parse_tick_file(TICK_FILE.encode("big5").decode("cp950")))
    day = minutes[("TXFK6", "regular")]
    assert [bar["time"] for bar in day] == [_ts("2026-10-30", "08:45"), _ts("2026-10-30", "08:46")]
    first = day[0]
    assert (first["open"], first["high"], first["low"], first["close"]) == (23000, 23010, 22990, 22990)
    assert first["volume"] == 8  # (10 + 4 + 2) / 2
    assert day[1]["volume"] == 3
    # 15:00 on Thursday and 01:00 on Friday morning are the night session.
    night = minutes[("MXFK6", "afterhours")]
    assert [bar["time"] for bar in night] == [_ts("2026-10-29", "15:00"), _ts("2026-10-30", "01:00")]
    assert [bar["volume"] for bar in night] == [2, 1]
    assert ("TXFL6", "regular") in minutes
    # 14:05 is outside both sessions and is dropped.
    assert all(bar["time"] != _ts("2026-10-30", "14:05") for bar in day)


def test_tick_file_without_header_is_a_format_error():
    with pytest.raises(TaifexFormatError):
        parse_tick_file("not,a,taifex,file\n1,2,3,4\n")


# --------------------------------------------------------------------------- import
def test_daily_import_is_idempotent_and_keeps_fubon_bars():
    repo = FakeRepository()
    monday = _ts("2026-11-02", "00:00")
    repo.upsert_bars(
        market="TWFutures", symbol="TXFK6", session="afterhours", timeframe="1D",
        bars=[{"time": monday, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 9}], source="fubon",
    )
    importer = TaifexHistoryImporter(repository=repo, fetcher=lambda *a: b"", is_trading_day=weekdays())
    first = importer.import_daily_text(DAILY_REPORT, products=["TX"])
    snapshot = dict(repo.bars)
    second = importer.import_daily_text(DAILY_REPORT, products=["TX"])
    assert repo.bars == snapshot
    assert first.written == second.written
    assert repo.get("TXFK6", "afterhours", "1D", monday)["source"] == "fubon"
    regular = repo.get("TXFK6", "regular", "1D", _ts("2026-10-30", "00:00"))
    assert regular["source"] == "taifex" and regular["close"] == 23050


def test_tick_import_fills_gaps_without_overwriting_fubon_minutes():
    repo = FakeRepository()
    fubon_minute = {"time": _ts("2026-10-30", "08:45"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    repo.upsert_bars(market="TWFutures", symbol="TXFK6", session="regular", timeframe="1m",
                     bars=[fubon_minute], source="fubon")
    importer = TaifexHistoryImporter(repository=repo, fetcher=lambda *a: b"")
    importer.import_tick_bytes(TICK_FILE.encode("big5"), products=["TX"])
    importer.import_tick_bytes(TICK_FILE.encode("utf-8"), products=["TX"])
    assert repo.get("TXFK6", "regular", "1m", _ts("2026-10-30", "08:45"))["source"] == "fubon"
    gap = repo.get("TXFK6", "regular", "1m", _ts("2026-10-30", "08:46"))
    assert gap["source"] == "taifex" and gap["close"] == 23005
    assert not any(key[1].startswith("MXF") for key in repo.bars)


def test_daily_download_is_throttled_chunked_and_reports_failures():
    requests: List[Tuple[str, str, Dict[str, str]]] = []
    sleeps: List[float] = []

    def fetcher(method, url, form=None):
        requests.append((method, url, dict(form or {})))
        if form["queryStartDate"] == "2026/11/01":
            raise ConnectionError("timeout")
        if form["queryStartDate"] == "2026/09/15":
            return "查無資料".encode("big5")
        return DAILY_REPORT.encode("big5")

    repo = FakeRepository()
    importer = TaifexHistoryImporter(
        repository=repo, fetcher=fetcher, sleep=sleeps.append, delay=1.0, is_trading_day=weekdays(),
    )
    report = importer.import_daily(date(2026, 9, 15), date(2026, 11, 5), ["TXF"])
    assert [(m, u) for m, u, _ in requests] == [("POST", DAILY_URL)] * 3
    assert [form["queryStartDate"] for _, _, form in requests] == ["2026/09/15", "2026/10/01", "2026/11/01"]
    assert requests[1][2]["queryEndDate"] == "2026/10/31"
    assert requests[1][2]["commodity_id"] == "TX"
    assert sleeps == [1.0, 1.0]  # one pause between consecutive requests
    assert [item["chunk"] for item in report.failed] == ["TX 2026-11-01..2026-11-05"]
    assert report.empty == ["TX 2026-09-15..2026-09-30"]
    assert report.written["TXFK6/regular/1D"] == 1


def test_month_chunks_and_tick_url():
    assert month_chunks(date(2025, 12, 20), date(2026, 2, 3)) == [
        (date(2025, 12, 20), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 3)),
    ]
    assert tick_file_url(date(2026, 10, 30)).endswith("/Daily_2026_10_30.zip")


# --------------------------------------------------------------------------- backfill
class FakeImporter:
    def __init__(self, fail: Tuple[date, ...] = ()) -> None:
        self.fail = set(fail)
        self.days: List[date] = []

    def import_tick_day(self, day, products):
        self.days.append(day)
        if day in self.fail:
            raise ConnectionError("file not published")
        from app.services.taifex_history import ImportReport

        report = ImportReport()
        report.add("TXFK6", "regular", "1m", 300)
        return report


class UnreachableOpenApi:
    """OpenAPI client whose probe fails: every day falls back to the web tick file."""

    def probe(self, endpoint):
        raise ConnectionError("offline")


def test_backfill_imports_previous_trading_days_and_retries_failures():
    repo = FakeRepository()
    importer = FakeImporter(fail=(date(2026, 10, 30),))
    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)  # Monday 09:00 Taipei
    result = taifex_tick_backfill.run_backfill(
        now, importer=importer, repository=repo, client=UnreachableOpenApi(),
        products=["TX"], is_trading_day=weekdays(), lookback=3,
    )
    assert importer.days == [date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)]
    assert [item["trade_date"] for item in result["imported"]] == ["2026-10-28", "2026-10-29"]
    assert [item["trade_date"] for item in result["failed"]] == ["2026-10-30"]
    assert repo.statuses[("TWFutures", "TAIFEX_TICKS", "all", date(2026, 10, 30))] == "failed"

    retry = FakeImporter()
    taifex_tick_backfill.run_backfill(
        now, importer=retry, repository=repo, client=UnreachableOpenApi(),
        products=["TX"], is_trading_day=weekdays(), lookback=3,
    )
    assert retry.days == [date(2026, 10, 30)]  # successful days are not downloaded again


def test_backfill_entry_point_skips_without_fubon(monkeypatch):
    monkeypatch.setenv("TAIFEX_TICK_BACKFILL_ENABLED", "false")
    assert "skipped" in taifex_tick_backfill.run_taifex_tick_backfill()
    monkeypatch.setenv("TAIFEX_TICK_BACKFILL_ENABLED", "true")
    from app.config.data_sources import FubonSidecarConfig

    monkeypatch.setattr(type(FubonSidecarConfig), "CONFIGURED", property(lambda cls: False))
    assert taifex_tick_backfill.run_taifex_tick_backfill() == {"skipped": "Fubon sidecar is not configured"}
    assert taifex_tick_backfill.configured_products("TXF,MTX") == ["TX", "MTX"]


def test_backfill_task_is_scheduled():
    from app.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule["taifex-tick-backfill"]
    assert schedule["task"] == "quantdinger.tasks.taifex_tick_backfill"
    assert celery_app.conf.task_routes["quantdinger.tasks.taifex_tick_backfill"] == {"queue": "maintenance"}


# --------------------------------------------------------------------------- CLI
def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_taifex_history.py"
    spec = importlib.util.spec_from_file_location("import_taifex_history_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_imports_tick_directory_and_daily_range(tmp_path, capsys):
    script = _load_script()
    (tmp_path / "Daily_2026_10_30.csv").write_bytes(TICK_FILE.encode("big5"))
    (tmp_path / "notes.md").write_text("ignored", encoding="utf-8")
    repo = FakeRepository()
    importer = TaifexHistoryImporter(
        repository=repo, fetcher=lambda *a: DAILY_REPORT.encode("utf-8"), sleep=lambda s: None,
        is_trading_day=weekdays(),
    )
    code = script.run(
        ["--ticks", str(tmp_path), "--daily", "--from", "2026-10-01", "--to", "2026-10-31", "--products", "TX,MTX"],
        importer=importer,
    )
    assert code == 0
    output = capsys.readouterr().out
    assert '"ticks"' in output and '"daily"' in output
    assert repo.get("MXFK6", "afterhours", "1m", _ts("2026-10-29", "15:00"))["volume"] == 2
    assert repo.get("MXFK6", "regular", "1D", _ts("2026-10-30", "00:00"))["close"] == 23052


def test_cli_rejects_bad_arguments():
    script = _load_script()
    with pytest.raises(SystemExit):
        script.run([], importer=TaifexHistoryImporter(repository=FakeRepository(), fetcher=lambda *a: b""))
    with pytest.raises(SystemExit):
        script.run(["--daily", "--from", "2026-10-01"], importer=None)
    with pytest.raises(SystemExit):
        script.run(["--daily", "--from", "2026-10-01", "--to", "2026-10-31", "--products", "XYZ"])
