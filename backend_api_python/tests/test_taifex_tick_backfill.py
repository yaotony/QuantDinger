"""TAIFEX tick backfill: OpenAPI ``TimeAndSalesData`` for the served day, web ZIP for the rest.

No network: the OpenAPI client gets a chunked in-memory fetcher and the history
importer an in-memory ZIP fetcher.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.services import taifex_tick_backfill as backfill
from app.services.taifex_history import TaifexHistoryImporter, parse_tick_file, ticks_to_minute_bars
from app.services.taifex_openapi import RawResponse, TaifexOpenApiClient

TAIPEI = ZoneInfo("Asia/Taipei")
STATUS_KEY = ("TWFutures", "TAIFEX_TICKS", "all")


def _ts(day: str, clock: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI).timestamp())


def weekdays(*holidays: str):
    closed = {date.fromisoformat(item) for item in holidays}
    return lambda day: day.weekday() < 5 and day not in closed


# --------------------------------------------------------------------------- fixtures
# Trading day Friday 2026-10-30: the night session opened Thursday 10-29 15:00.
# (Date, product, month, time, price, B+S volume)
TRADES: List[Tuple[str, str, str, str, str, str]] = [
    ("20261029", "TX", "202611", "150000", "22980", "4"),
    ("20261029", "TX", "202611", "235930", "22985", "2"),
    ("20261030", "TX", "202611", "010059", "22990", "2"),
    ("20261030", "TX", "202611", "084500", "23000", "10"),
    ("20261030", "TX", "202611", "084512", "23010", "4"),
    ("20261030", "TX", "202611", "084559", "22990", "2"),
    ("20261030", "TX", "202611", "84600", "23005", "6"),
    ("20261030", "TX", "202611W1", "084510", "23500", "8"),
    ("20261030", "TX", "202611/202612", "084520", "60", "8"),
    ("20261030", "MTX", "202611", "084530", "23001", "2"),
    ("20261030", "TE", "202611", "084500", "1200", "2"),
    ("20261030", "TXO", "202611", "084500", "100", "2"),
]


def openapi_rows(trades=TRADES) -> List[Dict[str, str]]:
    return [
        {
            "Date": day, "ProductCode": code, "ContractMonth(Week)": month, "TimeOfTrades": clock,
            "TradePrice": price, "Volume(Buy+Sell)": volume, "NearMonthPrice": "-", "FarMonthPrice": "-",
        }
        for day, code, month, clock, price, volume in trades
    ]


TICK_HEADER = "成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),近月價格,遠月價格,開盤集合競價 "


def web_zip(trades=TRADES, name: str = "Daily_2026_10_30.csv") -> bytes:
    lines = [TICK_HEADER] + [
        f"{day},{code:<8},{month:<11},{clock},{price},{volume},-,-," for day, code, month, clock, price, volume in trades
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, "\n".join(lines).encode("big5"))
    return buffer.getvalue()


class ChunkFeed:
    """Fetcher serving a JSON body in small chunks; counts chunks read and streams closed."""

    def __init__(self, rows: List[Dict[str, Any]], chunk_size: int = 16) -> None:
        self.payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        self.chunk_size = chunk_size
        self.requests: List[str] = []
        self.chunks_read = 0
        self.closed = 0

    @property
    def total_chunks(self) -> int:
        return -(-len(self.payload) // self.chunk_size)

    def __call__(self, method: str, url: str) -> RawResponse:
        self.requests.append(method)

        def chunks():
            try:
                for start in range(0, len(self.payload), self.chunk_size):
                    self.chunks_read += 1
                    yield self.payload[start:start + self.chunk_size]
            finally:
                self.closed += 1

        headers = {"Content-Length": str(len(self.payload)), "Last-Modified": "Thu, 29 Oct 2026 22:37:51 GMT"}
        return RawResponse(headers=headers, chunks=chunks())


def openapi_client(rows=None, chunk_size: int = 16) -> Tuple[TaifexOpenApiClient, ChunkFeed]:
    feed = ChunkFeed(openapi_rows() if rows is None else rows, chunk_size)
    return TaifexOpenApiClient(base_url="http://openapi.test", fetcher=feed), feed


class FakeRepository:
    """In-memory ``qd_tw_klines`` + status table with the fubon-wins upsert rule."""

    def __init__(self) -> None:
        self.bars: Dict[Tuple[str, str, str, str, int], Dict[str, Any]] = {}
        self.statuses: Dict[Tuple[str, str, str, date], str] = {}
        self.details: Dict[date, str] = {}

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
        self.details[trade_date] = error

    def status(self, day: date) -> Optional[str]:
        return self.statuses.get((*STATUS_KEY, day))

    def get(self, symbol, session, ts):
        return self.bars.get(("TWFutures", symbol, session, "1m", ts))


class ZipFetcher:
    """History-importer fetcher: ``Daily_YYYY_MM_DD.zip`` per day, or an HTTP-like failure."""

    def __init__(self, files: Optional[Dict[str, bytes]] = None) -> None:
        self.files = files or {}
        self.urls: List[str] = []

    def __call__(self, method, url, form=None):
        self.urls.append(url)
        for name, payload in self.files.items():
            if url.endswith(f"/Daily_{name}.zip"):
                return payload
        raise ConnectionError("404 Not Found")

    def days(self) -> List[str]:
        return [url.rsplit("/Daily_", 1)[1][:-4] for url in self.urls]


def importer_for(repo: FakeRepository, fetcher: ZipFetcher, check=None) -> TaifexHistoryImporter:
    return TaifexHistoryImporter(repository=repo, fetcher=fetcher, delay=0, is_trading_day=check or weekdays())


# Monday 2026-11-02 09:00 Taipei: pending days 10/28, 10/29, 10/30 with lookback 3.
MONDAY = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- parsing
def test_openapi_rows_parse_sessions_filters_and_halves_volume():
    ticks = list(backfill.iter_openapi_ticks(openapi_rows(), products=["TX", "MTX"]))
    assert {tick.symbol for tick in ticks} == {"TXFK6", "MXFK6"}  # TE filtered, TXO unknown
    assert all(tick.price not in (23500, 60) for tick in ticks)  # weekly and spread rows skipped
    night = [tick for tick in ticks if tick.symbol == "TXFK6"][:3]
    # Night ticks keep their calendar date: before and after midnight.
    assert [tick.ts for tick in night] == [
        _ts("2026-10-29", "15:00"), _ts("2026-10-29", "23:59:30"), _ts("2026-10-30", "01:00:59"),
    ]
    first_day = next(tick for tick in ticks if tick.ts == _ts("2026-10-30", "08:45"))
    assert first_day.contracts == 5  # B+S 10 -> 5 contracts
    assert any(tick.ts == _ts("2026-10-30", "08:46") for tick in ticks)  # "84600" without leading zero

    minutes = ticks_to_minute_bars(ticks)
    assert [bar["time"] for bar in minutes[("TXFK6", "afterhours")]] == [
        _ts("2026-10-29", "15:00"), _ts("2026-10-29", "23:59"), _ts("2026-10-30", "01:00"),
    ]
    regular = minutes[("TXFK6", "regular")]
    assert (regular[0]["open"], regular[0]["high"], regular[0]["low"], regular[0]["close"]) == (23000, 23010, 22990, 22990)
    assert regular[0]["volume"] == 8  # (10 + 4 + 2) / 2


def test_openapi_rows_skip_missing_values():
    rows = openapi_rows([("20261030", "TX", "202611", "084500", "-", "2"), ("20261030", "TX", "202611", "", "1", "2")])
    assert list(backfill.iter_openapi_ticks(rows, products=["TX"])) == []


def test_openapi_ticks_stream_without_loading_the_whole_response():
    client, feed = openapi_client(chunk_size=8)
    response = client.fetch("TimeAndSalesData")
    stream = backfill.iter_openapi_ticks(response, products=["TX"])
    first = next(stream)
    assert first.ts == _ts("2026-10-29", "15:00")
    assert feed.chunks_read < feed.total_chunks // 4  # the first tick arrived after a few chunks
    rest = list(stream)
    assert feed.chunks_read == feed.total_chunks
    expected = list(backfill.iter_openapi_ticks(json.loads(feed.payload), products=["TX"]))
    assert [first, *rest] == expected
    assert response.data_date == date(2026, 10, 30)


def test_openapi_and_web_zip_parse_the_same_ticks():
    from_api = list(backfill.iter_openapi_ticks(openapi_rows(), products=["TX", "MTX"]))
    from app.services.taifex_history import decode_taifex_bytes

    from_zip = parse_tick_file(decode_taifex_bytes(web_zip()), products=["TX", "MTX"])
    assert from_api == from_zip


# --------------------------------------------------------------------------- probe
def test_probe_reads_only_the_first_row_and_closes_the_stream():
    client, feed = openapi_client(chunk_size=8)
    probe = client.probe("TimeAndSalesData")
    assert probe.first_date == date(2026, 10, 29)  # night session before midnight
    assert probe.size == len(feed.payload)
    assert probe.last_modified is not None
    assert feed.chunks_read < feed.total_chunks // 4
    assert feed.closed == 1
    assert backfill.served_trading_day(probe.first_row, weekdays()) == date(2026, 10, 30)


def test_probe_of_an_empty_response():
    client, _ = openapi_client(rows=[])
    probe = client.probe("TimeAndSalesData")
    assert probe.empty and probe.first_date is None
    assert backfill.served_trading_day(probe.first_row, weekdays()) is None


def test_served_trading_day_skips_holidays_and_keeps_day_rows():
    check = weekdays("2026-10-23", "2026-10-26")
    night = {"Date": "20261022", "TimeOfTrades": "150000"}
    after_midnight = {"Date": "20261023", "TimeOfTrades": "003000"}
    day = {"Date": "20261027", "TimeOfTrades": "084500"}
    assert backfill.served_trading_day(night, check) == date(2026, 10, 27)
    assert backfill.served_trading_day(after_midnight, check) == date(2026, 10, 27)
    assert backfill.served_trading_day(day, check) == date(2026, 10, 27)


# --------------------------------------------------------------------------- backfill
def test_latest_day_uses_openapi_and_older_days_use_web_zip():
    repo = FakeRepository()
    fubon_minute = {"time": _ts("2026-10-30", "08:45"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    repo.upsert_bars(market="TWFutures", symbol="TXFK6", session="regular", timeframe="1m",
                     bars=[fubon_minute], source="fubon")
    client, feed = openapi_client()
    zips = ZipFetcher({
        "2026_10_28": web_zip([("20261028", "TX", "202611", "090000", "22900", "2")]),
        "2026_10_29": web_zip([("20261029", "TX", "202611", "090000", "22950", "2")]),
    })
    result = backfill.run_backfill(
        MONDAY, importer=importer_for(repo, zips), repository=repo, client=client,
        products=["TX"], is_trading_day=weekdays(), lookback=3,
    )
    assert result["served"] == "2026-10-30"
    assert [(item["trade_date"], item["source"]) for item in result["imported"]] == [
        ("2026-10-28", "web_zip"), ("2026-10-29", "web_zip"), ("2026-10-30", "openapi"),
    ]
    assert zips.days() == ["2026_10_28", "2026_10_29"]  # the served day is not downloaded from the web
    assert feed.requests == ["GET", "GET"]  # probe + full download
    assert repo.details[date(2026, 10, 30)] == "source=openapi"
    assert repo.details[date(2026, 10, 28)] == "source=web_zip"
    assert all(repo.status(date(2026, 10, d)) == "success" for d in (28, 29, 30))
    # The Fubon minute stays; the gaps around it come from TAIFEX.
    assert repo.get("TXFK6", "regular", _ts("2026-10-30", "08:45"))["source"] == "fubon"
    assert repo.get("TXFK6", "regular", _ts("2026-10-30", "08:46"))["close"] == 23005
    night = repo.get("TXFK6", "afterhours", _ts("2026-10-30", "01:00"))
    assert night["source"] == "taifex" and night["volume"] == 1
    assert repo.get("TXFK6", "afterhours", _ts("2026-10-29", "15:00"))["volume"] == 2
    assert not any(key[1].startswith("MXF") for key in repo.bars)  # MTX not requested


def test_rerun_is_idempotent_and_skips_finished_days():
    repo = FakeRepository()
    client, feed = openapi_client()
    zips = ZipFetcher({"2026_10_29": web_zip()})
    kwargs = dict(repository=repo, products=["TX"], is_trading_day=weekdays(), lookback=2)
    backfill.run_backfill(MONDAY, importer=importer_for(repo, zips), client=client, **kwargs)
    snapshot = dict(repo.bars)
    requests = len(feed.requests)

    again = backfill.run_backfill(MONDAY, importer=importer_for(repo, zips), client=client, **kwargs)
    assert again["skipped"] == "every recent trading day already backfilled"
    assert len(feed.requests) == requests  # not even probed

    # Re-importing the same day writes the same bars.
    repo.statuses.clear()
    client2, _ = openapi_client()
    backfill.run_backfill(MONDAY, importer=importer_for(repo, zips), client=client2, **kwargs)
    assert repo.bars == snapshot


def test_both_sources_produce_the_same_minute_bars():
    via_api = FakeRepository()
    client, _ = openapi_client()
    backfill.run_backfill(
        MONDAY, importer=importer_for(via_api, ZipFetcher()), repository=via_api, client=client,
        products=["TX", "MTX"], is_trading_day=weekdays(), lookback=1,
    )
    via_web = FakeRepository()
    stale, _ = openapi_client(rows=openapi_rows([("20261028", "TX", "202611", "084500", "1", "2")]))
    result = backfill.run_backfill(
        MONDAY, importer=importer_for(via_web, ZipFetcher({"2026_10_30": web_zip()})), repository=via_web,
        client=stale, products=["TX", "MTX"], is_trading_day=weekdays(), lookback=1,
    )
    assert result["imported"][0]["source"] == "web_zip"
    assert via_api.bars and via_api.bars == via_web.bars


class StubClient:
    def __init__(self, first_row: Optional[Dict[str, str]] = None, error: Optional[Exception] = None) -> None:
        self.first_row = first_row
        self.error = error

    def probe(self, endpoint):
        if self.error is not None:
            raise self.error
        from app.services.taifex_openapi import ProbeResult

        return ProbeResult(endpoint, self.first_row, None, None, None)

    def fetch(self, endpoint):
        raise AssertionError("no full download expected")


class StubImporter:
    def __init__(self, fail: Tuple[date, ...] = ()) -> None:
        self.fail = set(fail)
        self.days: List[date] = []

    def import_tick_day(self, day, products):
        self.days.append(day)
        if day in self.fail:
            raise ConnectionError("404 Not Found")
        from app.services.taifex_history import ImportReport

        report = ImportReport()
        report.add("TXFK6", "regular", "1m", 10)
        return report


def test_days_past_the_web_retention_are_missed():
    repo = FakeRepository()
    importer = StubImporter()
    now = datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc)  # Tue 2026-12-01 Taipei
    # The OpenAPI serves Monday 11-30; it is already collected.
    repo.statuses[(*STATUS_KEY, date(2026, 11, 30))] = "success"
    client = StubClient({"Date": "20261130", "TimeOfTrades": "084500"})
    result = backfill.run_backfill(
        now, importer=importer, repository=repo, client=client,
        products=["TX"], is_trading_day=weekdays(), lookback=25,
    )
    missed = [item["trade_date"] for item in result["missed"]]
    assert missed == ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]  # > 30 calendar days back
    assert importer.days[0] == date(2026, 11, 2) and date(2026, 11, 30) not in importer.days
    assert {item["source"] for item in result["imported"]} == {"web_zip"}
    assert repo.status(date(2026, 10, 27)) == "missed"
    assert "import_taifex_history.py" in repo.details[date(2026, 10, 27)]

    # Missed days are final: the next run does not warn or download again.
    second = backfill.run_backfill(
        now, importer=StubImporter(), repository=repo, client=client,
        products=["TX"], is_trading_day=weekdays(), lookback=25,
    )
    assert second["skipped"] == "every recent trading day already backfilled"


def test_web_zip_failure_is_retried_and_probe_failure_falls_back_to_web():
    repo = FakeRepository()
    importer = StubImporter(fail=(date(2026, 10, 30),))
    result = backfill.run_backfill(
        MONDAY, importer=importer, repository=repo, client=StubClient(error=ConnectionError("timeout")),
        products=["TX"], is_trading_day=weekdays(), lookback=2,
    )
    assert result["served"] is None
    assert importer.days == [date(2026, 10, 29), date(2026, 10, 30)]
    assert repo.status(date(2026, 10, 30)) == "failed"
    detail = repo.details[date(2026, 10, 30)]
    assert detail.startswith("source=web_zip: 404 Not Found") and "probe failed" in detail

    retry = StubImporter()
    backfill.run_backfill(
        MONDAY, importer=retry, repository=repo, client=StubClient(error=ConnectionError("timeout")),
        products=["TX"], is_trading_day=weekdays(), lookback=2,
    )
    assert retry.days == [date(2026, 10, 30)]
    assert repo.status(date(2026, 10, 30)) == "success"


def test_openapi_day_switching_during_download_is_retried():
    repo = FakeRepository()
    # The probe says 10/30, but the full download already holds a later day.
    client, _ = openapi_client(rows=openapi_rows([("20261102", "TX", "202611", "084500", "1", "2")]))
    client.probe = lambda endpoint: StubClient({"Date": "20261030", "TimeOfTrades": "084500"}).probe(endpoint)
    result = backfill.run_backfill(
        MONDAY, importer=importer_for(repo, ZipFetcher()), repository=repo, client=client,
        products=["TX"], is_trading_day=weekdays(), lookback=1,
    )
    assert result["failed"][0]["source"] == "openapi"
    assert "now serves 2026-11-02" in repo.details[date(2026, 10, 30)]
    assert repo.status(date(2026, 10, 30)) == "failed" and not repo.bars


def test_openapi_without_trades_for_the_products_fails():
    repo = FakeRepository()
    client, _ = openapi_client(rows=openapi_rows([("20261030", "TE", "202611", "084500", "1200", "2")]))
    result = backfill.run_backfill(
        MONDAY, importer=importer_for(repo, ZipFetcher()), repository=repo, client=client,
        products=["TX"], is_trading_day=weekdays(), lookback=1,
    )
    assert repo.status(date(2026, 10, 30)) == "failed"
    assert "no trades for TX" in result["failed"][0]["error"]
