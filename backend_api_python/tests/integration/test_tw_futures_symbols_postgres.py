"""TWFutures symbol catalog against PostgreSQL (issue #70).

Installs the bundled master seed (which creates the alias table), syncs a faked
sidecar FUTURE list and checks the queries behind
``/api/agent/v1/markets/TWFutures/symbols``: continuous codes first, contract
months next, colloquial 台指 aliases, and expired contracts deactivated on the
next sync.
"""

import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest

import app.utils.db as db_module
from app.data.market_symbols_seed import get_symbol_name, search_symbols
from app.services import symbol_master_sync


MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

MARKET_SYMBOLS_DDL = """
CREATE TABLE IF NOT EXISTS qd_market_symbols (
    id SERIAL PRIMARY KEY,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    name VARCHAR(255) DEFAULT '',
    exchange VARCHAR(50) DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    instrument_id VARCHAR(120) NOT NULL DEFAULT '',
    settle_currency VARCHAR(20) NOT NULL DEFAULT '',
    asset_class VARCHAR(20) NOT NULL DEFAULT 'crypto',
    product_type VARCHAR(32) NOT NULL DEFAULT 'crypto',
    api_family VARCHAR(24) NOT NULL DEFAULT 'spot',
    underlying_market VARCHAR(32) NOT NULL DEFAULT '',
    underlying_symbol VARCHAR(50) NOT NULL DEFAULT '',
    product_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_updated_at TIMESTAMPTZ,
    currency VARCHAR(10) DEFAULT '',
    is_active INTEGER DEFAULT 1,
    is_hot INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(market, symbol, exchange, market_type, instrument_id)
);
"""


class _CursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("?", "%s"), params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

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

    def rollback(self):
        self._raw.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self._raw.rollback()
        self._raw.close()


@pytest.fixture
def symbols_schema(monkeypatch):
    dsn = os.getenv("QD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("QD_TEST_POSTGRES_DSN is required")
    import psycopg2

    schema = f"qd_tw_futures_symbols_test_{uuid.uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", schema)
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute(MARKET_SYMBOLS_DDL)
            cursor.execute((MIGRATIONS / "market_symbols_master.sql").read_text(encoding="utf-8"))
        factory = lambda: _ConnectionAdapter(dsn, schema)  # noqa: E731
        monkeypatch.setattr(db_module, "get_db_connection", factory)
        monkeypatch.setattr(symbol_master_sync, "get_db_connection", factory)
        yield factory
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


class _Tickers:
    def __init__(self, rows):
        self.rows = rows

    def get_futopt_tickers(self, ticker_type="FUTURE"):
        return [dict(row) for row in self.rows]


NOVEMBER = [
    {"symbol": "TXFK6", "name": "臺股期貨115/11", "settlementDate": "2026-11-18"},
    {"symbol": "TXFL6", "name": "臺股期貨115/12", "settlementDate": "2026-12-16"},
    {"symbol": "MXFK6", "name": "小型臺指115/11", "settlementDate": "2026-11-18"},
    {"symbol": "TMFK6", "name": "微型臺指115/11", "settlementDate": "2026-11-18"},
]
DECEMBER = [
    {"symbol": "TXFK6", "name": "臺股期貨115/11", "settlementDate": "2026-11-18"},
    {"symbol": "TXFL6", "name": "臺股期貨115/12", "settlementDate": "2026-12-16"},
    {"symbol": "TXFA7", "name": "臺股期貨116/01", "settlementDate": "2027-01-20"},
    {"symbol": "MXFL6", "name": "小型臺指115/12", "settlementDate": "2026-12-16"},
    {"symbol": "TMFL6", "name": "微型臺指115/12", "settlementDate": "2026-12-16"},
]


def _sync(rows, as_of):
    symbol_master_sync.upsert_symbol_master(
        symbol_master_sync.fetch_tw_futures_symbols(_Tickers(rows), as_of=as_of)
    )


def _active_symbols(factory):
    with factory() as db:
        cur = db.cursor()
        cur.execute("SELECT symbol FROM qd_market_symbols WHERE market = 'TWFutures' AND is_active = 1")
        symbols = {row["symbol"] for row in cur.fetchall()}
        cur.close()
    return symbols


@pytest.mark.integration
def test_synced_twfutures_catalog_is_searchable(symbols_schema):
    _sync(NOVEMBER, datetime(2026, 11, 2, 9, 0))

    assert _active_symbols(symbols_schema) == {"TXF", "MXF", "TMF", "TXFK6", "TXFL6", "MXFK6", "TMFK6"}
    by_prefix = [row["symbol"] for row in search_symbols("TWFutures", "TX")]
    assert by_prefix == ["TXF", "TXFK6", "TXFL6"]
    by_alias = [row["symbol"] for row in search_symbols("TWFutures", "台指")]
    assert by_alias[0] == "TXF"
    assert {"MXF", "TMF"} <= set(by_alias)
    assert by_alias.index("MXF") < by_alias.index("MXFK6")
    assert [row["symbol"] for row in search_symbols("TWFutures", "TXF")] == ["TXF"]
    assert [row["symbol"] for row in search_symbols("TWFutures", "TXFK6")] == ["TXFK6"]
    assert get_symbol_name("TWFutures", "TXF") == "臺股期貨"


@pytest.mark.integration
def test_rollover_sync_deactivates_expired_contracts_and_adds_new_ones(symbols_schema):
    _sync(NOVEMBER, datetime(2026, 11, 2, 9, 0))
    _sync(NOVEMBER, datetime(2026, 11, 2, 9, 0))  # repeated sync is idempotent
    _sync(DECEMBER, datetime(2026, 11, 18, 14, 0))

    active = _active_symbols(symbols_schema)
    assert active == {"TXF", "MXF", "TMF", "TXFL6", "TXFA7", "MXFL6", "TMFL6"}
    assert [row["symbol"] for row in search_symbols("TWFutures", "TXFK6")] == []
    assert [row["symbol"] for row in search_symbols("TWFutures", "TX")] == ["TXF", "TXFA7", "TXFL6"]
