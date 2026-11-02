"""TWStock symbol master against PostgreSQL (issue #55).

Installs the bundled master seed, then checks the queries behind
``/api/agent/v1/markets/TWStock/symbols`` and a repeated sidecar sync.
"""

import os
import re
import uuid
from pathlib import Path

import pytest

import app.utils.db as db_module
from app.data.market_symbols_seed import get_hot_symbols, get_symbol_name, search_symbols
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

    @property
    def rowcount(self):
        return self._cursor.rowcount

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

    schema = f"qd_tw_symbols_test_{uuid.uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", schema)
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute(MARKET_SYMBOLS_DDL)
            seed = (MIGRATIONS / "market_symbols_master.sql").read_text(encoding="utf-8")
            cursor.execute(seed)
            cursor.execute(seed)  # registered bootstrap components must be retry-safe
        factory = lambda: _ConnectionAdapter(dsn, schema)  # noqa: E731
        monkeypatch.setattr(db_module, "get_db_connection", factory)
        monkeypatch.setattr(symbol_master_sync, "get_db_connection", factory)
        yield factory
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


class _FakeTickerSource:
    TICKERS = {
        ("EQUITY", "TWSE", "TSE"): [
            {"symbol": "2330", "name": "台積電"},
            {"symbol": "0050", "name": "元大台灣50"},
            {"symbol": "030001", "name": "台積電元大5A購01"},
        ],
        ("EQUITY", "TPEx", "OTC"): [{"symbol": "6488", "name": "環球晶"}],
        ("INDEX", "TWSE", None): [{"symbol": "IX0001", "name": "發行量加權股價指數"}],
        ("INDEX", "TPEx", None): [{"symbol": "IX0043", "name": "櫃買指數"}],
    }

    def get_tickers(self, ticker_type, exchange=None, market=None):
        return list(self.TICKERS.get((ticker_type, exchange, market), []))


class _FailingTickerSource:
    def get_tickers(self, ticker_type, exchange=None, market=None):
        from app.data_sources.tw_stock import FubonSidecarError

        raise FubonSidecarError("Fubon market data sidecar is not ready (503 service unavailable)")


def _tw_rows(factory):
    with factory() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT symbol, exchange, asset_class, is_hot FROM qd_market_symbols WHERE market = 'TWStock' ORDER BY symbol"
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.close()
    return rows


@pytest.mark.integration
def test_seeded_twstock_symbols_are_searchable_by_name_and_code(symbols_schema):
    assert [row["symbol"] for row in search_symbols("TWStock", "台積")] == ["2330.TW"]
    assert [row["symbol"] for row in search_symbols("TWStock", "2330")] == ["2330.TW"]
    assert {row["symbol"] for row in get_hot_symbols("TWStock", limit=20)} == {
        "2330.TW", "2317.TW", "2454.TW", "0050.TW", "0056.TW", "IX0001",
    }
    assert get_symbol_name("TWStock", "2330") == "台積電"


@pytest.mark.integration
def test_repeated_sidecar_sync_is_idempotent_and_keeps_hot_flags(symbols_schema):
    rows = symbol_master_sync.fetch_tw_stock_symbols(_FakeTickerSource())

    symbol_master_sync.upsert_symbol_master(rows)
    first = _tw_rows(symbols_schema)
    symbol_master_sync.upsert_symbol_master(symbol_master_sync.fetch_tw_stock_symbols(_FakeTickerSource()))
    second = _tw_rows(symbols_schema)

    assert first == second
    by_symbol = {row["symbol"]: row for row in second}
    assert by_symbol["2330.TW"] == {"symbol": "2330.TW", "exchange": "TWSE", "asset_class": "equity", "is_hot": 1}
    assert by_symbol["0050.TW"]["asset_class"] == "etf"
    assert by_symbol["IX0001"]["asset_class"] == "index"
    assert by_symbol["IX0001"]["is_hot"] == 1
    assert by_symbol["6488.TWO"]["exchange"] == "TPEx"
    assert by_symbol["IX0043"]["exchange"] == "TPEx"
    assert not any(row["symbol"].startswith("030001") for row in second)
    assert [row["symbol"] for row in search_symbols("TWStock", "環球")] == ["6488.TWO"]


def _active_flags(factory):
    with factory() as db:
        cur = db.cursor()
        cur.execute("SELECT symbol, is_active, is_hot FROM qd_market_symbols WHERE market = 'TWStock'")
        rows = {row["symbol"]: (row["is_active"], row["is_hot"]) for row in cur.fetchall()}
        cur.close()
    return rows


@pytest.mark.integration
def test_full_sync_deactivates_missing_codes_and_keeps_hot_flags(symbols_schema):
    def full_sync():
        rows = symbol_master_sync.fetch_tw_stock_symbols(_FakeTickerSource())
        symbol_master_sync.upsert_symbol_master(rows, deactivate_missing_tw_stock=True)

    full_sync()
    first = _active_flags(symbols_schema)
    full_sync()
    second = _active_flags(symbols_schema)

    assert first == second
    assert second["2330.TW"] == (1, 1)
    assert second["IX0001"] == (1, 1)
    assert second["6488.TWO"][0] == 1
    # seeded codes absent from the listing are switched off, hot flag kept
    assert second["2317.TW"] == (0, 1)
    assert "2317.TW" not in {row["symbol"] for row in get_hot_symbols("TWStock", limit=20)}


@pytest.mark.integration
def test_failing_sidecar_deactivates_nothing(symbols_schema, monkeypatch):
    from app.services import market_catalog_sync
    from app.utils import config_loader

    before = _active_flags(symbols_schema)
    monkeypatch.setattr(config_loader, "load_addon_config", lambda: {"fubon_sidecar": {"token": "test-key"}})
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_stock_symbols",
        lambda: symbol_master_sync.fetch_tw_stock_symbols(_FailingTickerSource()),
    )

    result = market_catalog_sync._sync_tw_stock_catalog()

    assert result["ok"] is False
    assert _active_flags(symbols_schema) == before
