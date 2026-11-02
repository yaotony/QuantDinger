"""Taiwan K-line storage against PostgreSQL (issue #56).

Applies the 20261102 migration twice in a throwaway schema, then checks the
idempotent upsert, the fubon-over-taifex rule, range / latest-N queries, the
earliest bar and the collection status table.
"""

import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import app.utils.db as db_module
from app.services.tw_kline_store import TWKlineRepository

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "20261102_tw_klines.sql"


class _CursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("?", "%s"), params)
        return self

    def executemany(self, sql, rows):
        from psycopg2.extras import execute_batch

        execute_batch(self._cursor, sql.replace("?", "%s"), rows)
        return self

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def close(self):
        self._cursor.close()


class _ConnectionAdapter:
    def __init__(self, dsn, schema):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        self._raw = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
        with self._raw.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        self._raw.commit()

    def cursor(self):
        return _CursorAdapter(self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self._raw.rollback()
        self._raw.close()


@pytest.fixture
def tw_kline_schema(monkeypatch):
    dsn = os.getenv("QD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("QD_TEST_POSTGRES_DSN is required")
    import psycopg2

    schema = f"qd_tw_klines_test_{uuid.uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", schema)
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
            sql = MIGRATION.read_text(encoding="utf-8")
            cursor.execute(sql)
            cursor.execute(sql)  # registered migrations must be retry-safe
            cursor.execute(
                """
                CREATE TABLE qd_watchlist (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER,
                    market VARCHAR(50) NOT NULL,
                    symbol VARCHAR(50) NOT NULL
                )
                """
            )
        monkeypatch.setattr(db_module, "get_db_connection", lambda: _ConnectionAdapter(dsn, schema))
        yield lambda: _ConnectionAdapter(dsn, schema)
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _ts(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def _bar(text, close, volume=10.0):
    return {"time": _ts(text), "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
            "volume": volume}


def _count(connect, where=""):
    with connect() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS n FROM qd_tw_klines {where}")
        row = cur.fetchone()
        cur.close()
    return int(row["n"])


STOCK = {"market": "TWStock", "symbol": "2330", "session": "regular", "timeframe": "1m"}


@pytest.mark.integration
def test_upsert_is_idempotent_and_queries_ranges(tw_kline_schema):
    repo = TWKlineRepository()
    bars = [_bar(f"2026-10-28T01:0{i}:00", 100 + i) for i in range(5)]

    assert repo.upsert_bars(**STOCK, bars=bars, source="fubon") == 5
    assert repo.upsert_bars(**STOCK, bars=bars, source="fubon") == 5  # rerun
    assert _count(tw_kline_schema) == 5

    # A corrected bar replaces the stored values instead of adding a row.
    repo.upsert_bars(**STOCK, bars=[_bar("2026-10-28T01:04:00", 999)], source="fubon")
    assert _count(tw_kline_schema) == 5

    window = repo.load_bars(**STOCK, start_ts=_ts("2026-10-28T01:01:00"), end_ts=_ts("2026-10-28T01:04:00"))
    assert [b["time"] for b in window] == [_ts(f"2026-10-28T01:0{i}:00") for i in (1, 2, 3)]

    latest = repo.load_bars(**STOCK, end_ts=_ts("2026-10-28T02:00:00"), limit=2)
    assert [b["close"] for b in latest] == [103.0, 999.0]  # newest two, ascending
    assert latest[0] == {"time": _ts("2026-10-28T01:03:00"), "open": 102.0, "high": 104.0, "low": 101.0,
                         "close": 103.0, "volume": 10.0}

    assert repo.earliest_bar_time(**STOCK) == _ts("2026-10-28T01:00:00")
    assert repo.earliest_bar_time(market="TWStock", symbol="0050", session="regular", timeframe="1m") is None


@pytest.mark.integration
def test_sessions_and_contracts_are_separate_keys(tw_kline_schema):
    repo = TWKlineRepository()
    bar = [_bar("2026-10-28T07:00:00", 23000)]
    for session in ("regular", "afterhours"):
        repo.upsert_bars(market="TWFutures", symbol="TXFK6", session=session, timeframe="1m",
                         bars=bar, source="fubon")
    repo.upsert_bars(market="TWFutures", symbol="TXFL6", session="afterhours", timeframe="1m",
                     bars=bar, source="fubon")

    assert _count(tw_kline_schema, "WHERE market = 'TWFutures'") == 3
    night = repo.load_bars(market="TWFutures", symbol="TXFK6", session="afterhours", timeframe="1m")
    assert len(night) == 1


@pytest.mark.integration
def test_fubon_bar_wins_over_taifex(tw_kline_schema):
    repo = TWKlineRepository()
    when = "2026-10-28T01:00:00"

    repo.upsert_bars(**STOCK, bars=[_bar(when, 100)], source="fubon")
    repo.upsert_bars(**STOCK, bars=[_bar(when, 555)], source="taifex")
    assert repo.load_bars(**STOCK)[0]["close"] == 100.0

    other = "2026-10-28T01:01:00"
    repo.upsert_bars(**STOCK, bars=[_bar(other, 555)], source="taifex")
    repo.upsert_bars(**STOCK, bars=[_bar(other, 101)], source="fubon")
    assert [b["close"] for b in repo.load_bars(**STOCK)] == [100.0, 101.0]
    with tw_kline_schema() as db:
        cur = db.cursor()
        cur.execute("SELECT DISTINCT source FROM qd_tw_klines")
        assert {row["source"] for row in cur.fetchall()} == {"fubon"}
        cur.close()


@pytest.mark.integration
def test_collection_status_and_watchlist(tw_kline_schema):
    repo = TWKlineRepository()
    day = date(2026, 10, 28)

    repo.record_status(market="TWStock", symbol="2330", session="regular", trade_date=day,
                       status="failed", error="Fubon market data sidecar is not ready")
    repo.record_status(market="TWStock", symbol="2330", session="regular", trade_date=day,
                       status="success", bar_count=270)
    repo.record_status(market="TWFutures", symbol="TXFK6", session="afterhours", trade_date=day,
                       status="missed", error="expired", attempted=False)

    statuses = repo.load_statuses(day)
    assert statuses == {
        ("TWStock", "2330", "regular", day): "success",
        ("TWFutures", "TXFK6", "afterhours", day): "missed",
    }
    assert repo.load_statuses(date(2026, 10, 29)) == {}
    with tw_kline_schema() as db:
        cur = db.cursor()
        cur.execute("SELECT attempts, bar_count, last_error FROM qd_tw_kline_collect_status WHERE symbol = '2330'")
        row = cur.fetchone()
        cur.close()
    assert (row["attempts"], row["bar_count"], row["last_error"]) == (2, 270, "")

    with tw_kline_schema() as db:
        cur = db.cursor()
        for user_id, market, symbol in ((1, "TWStock", "2330.TW"), (2, "TWStock", "2330.TW"),
                                        (2, "TWFutures", "TXF"), (3, "USStock", "AAPL")):
            cur.execute("INSERT INTO qd_watchlist (user_id, market, symbol) VALUES (?, ?, ?)",
                        (user_id, market, symbol))
        db.commit()
        cur.close()
    assert repo.watchlist_symbols() == [("TWFutures", "TXF"), ("TWStock", "2330.TW")]


@pytest.mark.integration
def test_provider_reads_stored_minutes(tw_kline_schema, monkeypatch):
    from app.data_sources.tw_stock import TWStockDataSource

    repo = TWKlineRepository()
    start = _ts("2025-03-03T01:00:00")
    repo.upsert_bars(**STOCK, bars=[
        {"time": start + 60 * i, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1} for i in range(10)
    ], source="fubon")
    source = TWStockDataSource(kline_store=repo)

    rows = source.get_kline("2330.TW", "5m", limit=10, before_time=start + 3600, after_time=start)

    assert [r["time"] for r in rows] == [start, start + 300]
    assert [r["volume"] for r in rows] == [5.0, 5.0]
    monkeypatch.setattr(source, "fetch_minute_bars", lambda code, include_intraday=True: [])
    assert source.get_earliest_kline_time("2330.TW", "5m") == start
