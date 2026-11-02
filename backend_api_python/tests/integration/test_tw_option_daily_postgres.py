"""TAIFEX option history storage against PostgreSQL (issue #67).

Applies the 20261103 migration twice in a throwaway schema, imports a parsed
daily report and settlement table twice, and checks the rows are upserted (no
duplicates, corrected values replace old ones) and read back for the backtester.
"""

import os
import re
import uuid
from datetime import date
from pathlib import Path

import pytest

import app.utils.db as db_module
from app.services.options_backtest import OptionMarket
from app.services.taifex_options_history import TaifexOptionsImporter
from app.services.tw_option_store import TWOptionRepository

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "20261103_tw_option_daily.sql"

DAILY = "\n".join([
    "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,交易時段",
    "2026/08/03,TXO,202608,23000,買權,95,110,90,100,1500,100,8000,一般",
    "2026/08/03,TXO,202608,23000,賣權,120,130,105,110,1300,110,7000,一般",
    "2026/08/03,TXO,202608,23000,買權,100,104,98,102,300,-,-,盤後",
    "2026/08/03,TXO,202609,24000,買權,-,-,-,-,0,35,120,一般",
])
SETTLEMENT = "最後結算日,契約月份,臺指選擇權(TXO)\n2026/08/19,202608,23150"


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
def option_schema(monkeypatch):
    dsn = os.getenv("QD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("QD_TEST_POSTGRES_DSN is required")
    import psycopg2

    schema = f"qd_tw_option_test_{uuid.uuid4().hex[:12]}"
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
        monkeypatch.setattr(db_module, "get_db_connection", lambda: _ConnectionAdapter(dsn, schema))
        yield lambda: _ConnectionAdapter(dsn, schema)
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _count(connect, table):
    with connect() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        row = cur.fetchone()
        cur.close()
    return int(row["n"])


@pytest.mark.integration
def test_option_import_is_idempotent_and_feeds_the_backtester(option_schema):
    repo = TWOptionRepository()
    importer = TaifexOptionsImporter(repository=repo, fetcher=lambda *a: b"", delay=0)
    for _ in range(2):
        importer.import_daily_bytes(DAILY.encode("cp950"), products=["TXO"])
        importer.import_settlement_bytes(SETTLEMENT.encode("utf-8"), products=["TXO"])
    assert _count(option_schema, "qd_tw_option_daily") == 4
    assert _count(option_schema, "qd_tw_option_settlement") == 1

    # A corrected settlement price replaces the stored value instead of adding a row.
    importer.import_daily_bytes(DAILY.replace(",100,8000,一般", ",101,8000,一般").encode("utf-8"))
    assert _count(option_schema, "qd_tw_option_daily") == 4

    rows = repo.load_daily(product="TXO", start=date(2026, 8, 1), end=date(2026, 8, 31))
    assert len(rows) == 3  # regular session only
    call = next(r for r in rows if r["expiry"] == "202608" and r["right"] == "call")
    assert call["settlement"] == 101.0 and call["close"] == 100.0 and call["open_interest"] == 8000.0
    idle = next(r for r in rows if r["expiry"] == "202609")
    assert idle["close"] is None and idle["settlement"] == 35.0

    settlements = repo.load_settlements(product="TXO")
    assert settlements == {"202608": (date(2026, 8, 19), 23150.0)}
    market = OptionMarket(rows, settlements, expiry_dates={"202608": date(2026, 8, 19)})
    assert market.final_settlement("202608") == 23150.0
    assert market.underlying(date(2026, 8, 3), "202608") == 23000 + 101 - 110
