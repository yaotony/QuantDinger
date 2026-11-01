"""PostgreSQL storage for perpetual funding rates (issue #31)."""

import os
import re
import uuid
from pathlib import Path

import pandas as pd
import pytest

import app.utils.db as db_module
from app.services.funding_rates import FundingRateRecord, FundingRateRepository


MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "20261101_perpetual_funding_rates.sql"


class _CursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("?", "%s"), params)
        return self

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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self._raw.rollback()
        self._raw.close()


@pytest.fixture
def funding_schema():
    dsn = os.getenv("QD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("QD_TEST_POSTGRES_DSN is required")
    import psycopg2

    schema = f"qd_funding_test_{uuid.uuid4().hex[:12]}"
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
        yield lambda: _ConnectionAdapter(dsn, schema)
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _record(hour, rate):
    return FundingRateRecord(
        exchange_id="binance",
        symbol="BTCUSDT",
        funding_time=pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
        rate=rate,
        mark_price=100.0,
    )


@pytest.mark.integration
def test_repository_deduplicates_settlements_and_loads_ranges(funding_schema, monkeypatch):
    monkeypatch.setattr(db_module, "get_db_connection", funding_schema)
    repository = FundingRateRepository()

    assert repository.upsert([_record(0, 0.0001), _record(4, 0.0002)], source="test") == 2
    # The same settlement again replaces the rate instead of adding a row.
    repository.upsert([_record(4, -0.0003), _record(8, 0.0001)], source="test")

    rows = repository.load(
        "binance",
        "BTCUSDT",
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-01 23:59:59"),
    )
    assert [(row.funding_time.hour, row.rate) for row in rows] == [
        (0, 0.0001),
        (4, -0.0003),
        (8, 0.0001),
    ]
    assert repository.load(
        "binance",
        "BTCUSDT",
        pd.Timestamp("2026-01-01 01:00"),
        pd.Timestamp("2026-01-01 05:00"),
    )[0].funding_time.hour == 4
    assert repository.load("binance", "ETHUSDT", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")) == []
