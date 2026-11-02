"""TWFutures symbol catalog sync and search (issue #70).

The Fubon sidecar and PostgreSQL are never contacted: ticker sources, the
database cursor and the search cache are faked.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from app.data_sources.tw_stock import FubonSidecarError
from app.services import market_catalog_sync, symbol_master_sync
from app.services.market import symbol_search
from app.services.symbol_master_sync import (
    FETCHERS,
    TW_FUTURES_PRODUCT_ALIASES,
    SymbolMasterRow,
    fetch_tw_futures_symbols,
    sync_symbol_master,
    upsert_symbol_master,
)

# A morning in the November 2026 contract month; TXFK6 expires 2026-11-18.
AS_OF = datetime(2026, 11, 2, 9, 0)

OCTOBER_LISTING = [
    {"symbol": "TXFJ6", "name": "臺股期貨115/10", "settlementDate": "2026-10-21", "endDate": "2026-10-21"},
    {"symbol": "TXFK6", "name": "臺股期貨115/11", "settlementDate": "2026-11-18", "endDate": "2026-11-18"},
    {"symbol": "TXFL6", "name": "臺股期貨115/12", "settlementDate": "2026-12-16", "endDate": "2026-12-16"},
    {"symbol": "MXFK6", "name": "小型臺指115/11", "settlementDate": "2026-11-18", "endDate": "2026-11-18"},
    {"symbol": "TMFK6", "name": "微型臺指115/11", "settlementDate": "2026-11-18"},
    {"symbol": "EXFK6", "name": "電子期貨115/11", "settlementDate": "2026-11-18"},
    {"symbol": "TXFK6/L6", "name": "臺股期貨價差"},
    {"symbol": "TXO18000K6", "name": "臺指選擇權"},
    {"symbol": "", "name": "blank"},
]


class _Tickers:
    def __init__(self, rows):
        self.rows = rows

    def get_futopt_tickers(self, ticker_type="FUTURE"):
        assert ticker_type == "FUTURE"
        return [dict(row) for row in self.rows]


class _UnavailableSource:
    def get_futopt_tickers(self, ticker_type="FUTURE"):
        raise FubonSidecarError("Fubon market data sidecar at http://sidecar is unreachable")


def _by_symbol(rows):
    return {row.symbol: row for row in rows}


@pytest.fixture(autouse=True)
def _fresh_addon_config(monkeypatch):
    """Re-read FUBON_SIDECAR_TOKEN per test and leave no cached config behind."""
    from app.utils import config_loader

    monkeypatch.setattr(config_loader, "_config_cache", None)


# --------------------------------------------------------------------------- ticker conversion


def test_tickers_become_product_and_contract_rows():
    rows = fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF)
    by_symbol = _by_symbol(rows)

    assert set(by_symbol) == {
        "TXF", "MXF", "TMF", "EXF",
        "TXFK6", "TXFL6", "MXFK6", "TMFK6", "EXFK6",
    }
    for row in rows:
        assert (row.market, row.exchange, row.currency) == ("TWFutures", "TAIFEX", "TWD")
        assert (row.asset_class, row.product_type, row.api_family) == ("futures", "futures", "futures")

    txf = by_symbol["TXF"]
    assert txf.name == "臺股期貨"
    assert txf.product_meta == {"product": "TXF", "multiplier": 200.0, "tickSize": 1.0, "continuous": True}
    assert by_symbol["TMF"].name == "微型臺指"
    assert by_symbol["TXFK6"].name == "臺股期貨115/11"
    assert by_symbol["TXFK6"].product_meta["settlementDate"] == "2026-11-18"
    assert by_symbol["TXFK6"].product_meta["endDate"] == "2026-11-18"
    assert "continuous" not in by_symbol["TXFK6"].product_meta


def test_product_rows_lead_the_catalog_ahead_of_contract_months():
    rows = fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF)
    symbols = [row.symbol for row in rows]

    assert symbols[:4] == ["TXF", "MXF", "TMF", "EXF"]
    assert all(len(symbol) == 5 for symbol in symbols[4:])


def test_product_and_contract_rows_carry_taiwanese_search_aliases():
    by_symbol = _by_symbol(fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF))

    assert set(TW_FUTURES_PRODUCT_ALIASES["TXF"]) <= set(by_symbol["TXF"].aliases)
    assert "台股期貨" in by_symbol["TXF"].aliases
    assert "小型台指" in by_symbol["MXF"].aliases
    assert "小台" in by_symbol["MXF"].aliases
    assert by_symbol["TXFK6"].aliases == ("台股期貨115/11",)
    # Names without 臺 need no spelling variant and the name itself is never an alias.
    assert by_symbol["EXFK6"].aliases == ()
    assert all(row.name not in row.aliases for row in by_symbol.values())


def test_unknown_products_are_listed_with_their_sidecar_name():
    rows = fetch_tw_futures_symbols(
        _Tickers([{"symbol": "CDFK6", "name": "台積電期貨115/11", "settlementDate": "2026-11-18"}]),
        as_of=AS_OF,
    )
    by_symbol = _by_symbol(rows)

    assert by_symbol["CDF"].name == "台積電期貨"
    assert by_symbol["CDF"].product_meta == {"product": "CDF", "continuous": True}
    assert by_symbol["CDF"].aliases == ()
    assert "CDFK6" in by_symbol


# --------------------------------------------------------------------------- expiry and rollover


def test_expired_contracts_are_excluded_but_the_product_stays():
    by_symbol = _by_symbol(fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF))

    assert "TXFJ6" not in by_symbol
    assert "TXF" in by_symbol


@pytest.mark.parametrize(
    ("as_of", "listed"),
    [
        (datetime(2026, 11, 18, 13, 29), True),   # last trading day, before the close
        (datetime(2026, 11, 18, 13, 30), False),  # day session closed
        (date(2026, 11, 18), True),
        (date(2026, 11, 19), False),
    ],
)
def test_contract_expires_after_its_last_trading_day_close(as_of, listed):
    by_symbol = _by_symbol(fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=as_of))

    assert ("TXFK6" in by_symbol) is listed
    assert "TXFL6" in by_symbol


def test_end_date_wins_over_settlement_date_and_missing_dates_keep_the_listing():
    rows = fetch_tw_futures_symbols(
        _Tickers([
            {"symbol": "TXFK6", "name": "臺股期貨115/11", "settlementDate": "2026-11-18", "endDate": "2026-11-01"},
            {"symbol": "TXFL6", "name": "臺股期貨115/12"},
            {"symbol": "MXFL6", "name": "小型臺指115/12", "endDate": "not-a-date"},
        ]),
        as_of=AS_OF,
    )

    assert {row.symbol for row in rows} == {"TXF", "MXF", "TXFL6", "MXFL6"}


def test_new_contract_month_appears_after_rollover():
    december = [
        {"symbol": "TXFL6", "name": "臺股期貨115/12", "settlementDate": "2026-12-16"},
        {"symbol": "TXFA7", "name": "臺股期貨116/01", "settlementDate": "2027-01-20"},
    ]
    before = _by_symbol(fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF))
    after = _by_symbol(fetch_tw_futures_symbols(_Tickers(december), as_of=datetime(2026, 11, 19, 9, 0)))

    assert "TXFA7" not in before
    assert {"TXF", "TXFL6", "TXFA7"} == set(after)


def test_default_as_of_is_now_in_taipei(monkeypatch):
    seen = []
    monkeypatch.setattr(
        symbol_master_sync, "_tw_futures_contract_expired", lambda raw, as_of: seen.append(as_of) or False,
    )

    fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING[:1]))

    assert seen and seen[0].tzinfo is not None
    assert seen[0].utcoffset().total_seconds() == 8 * 3600


# --------------------------------------------------------------------------- persistence


class _RecordingCursor:
    def __init__(self, statements):
        self.statements = statements

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))

    def close(self):
        return None


class _RecordingConnection:
    def __init__(self, statements):
        self.statements = statements
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def cursor(self):
        return _RecordingCursor(self.statements)

    def commit(self):
        self.committed = True


def test_upsert_deactivates_unlisted_contracts_and_writes_aliases(monkeypatch):
    statements = []
    connection = _RecordingConnection(statements)
    monkeypatch.setattr(symbol_master_sync, "get_db_connection", lambda: connection)
    rows = fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF)

    written = upsert_symbol_master(rows)

    assert written == len(rows)
    assert connection.committed
    assert statements[0][0] == "UPDATE qd_market_symbols SET is_active = 0 WHERE market = 'TWFutures'"
    upserted = [params[1] for sql, params in statements if sql.startswith("INSERT INTO qd_market_symbols ")]
    assert upserted == [row.symbol for row in rows]
    assert "TXFJ6" not in upserted
    aliases = {
        (params[1], params[2])
        for sql, params in statements
        if sql.startswith("INSERT INTO qd_market_symbol_aliases")
    }
    assert ("TXF", "台指期") in aliases
    assert ("MXF", "小型台指") in aliases
    assert ("TXFK6", "台股期貨115/11") in aliases
    assert all(
        params[0] == "TWFutures"
        for sql, params in statements
        if sql.startswith("INSERT INTO qd_market_symbol_aliases")
    )


def test_upsert_writes_no_aliases_for_rows_without_them(monkeypatch):
    statements = []
    monkeypatch.setattr(symbol_master_sync, "get_db_connection", lambda: _RecordingConnection(statements))

    upsert_symbol_master([SymbolMasterRow("TWStock", "2330.TW", "台積電", "TWSE", "TWD", asset_class="equity")])

    assert not any("qd_market_symbol_aliases" in sql for sql, _ in statements)
    assert not any("market = 'TWFutures'" in sql for sql, _ in statements)


# --------------------------------------------------------------------------- sidecar failures


def test_catalog_sync_skips_unreachable_sidecar_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "test-key")
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_futures_symbols",
        lambda: fetch_tw_futures_symbols(_UnavailableSource()),
    )
    monkeypatch.setattr(
        market_catalog_sync,
        "upsert_symbol_master",
        lambda rows: (_ for _ in ()).throw(AssertionError("nothing should be written")),
    )

    with caplog.at_level(logging.WARNING):
        result = market_catalog_sync._sync_tw_futures_catalog()

    assert result["ok"] is False
    assert result["skipped"] is True
    assert "unreachable" in result["error"]
    assert any("TWFutures catalog sync skipped" in record.getMessage() for record in caplog.records)


def test_catalog_sync_skips_twfutures_when_sidecar_is_not_configured(monkeypatch):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "")
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_futures_symbols",
        lambda: (_ for _ in ()).throw(AssertionError("sidecar should not be called")),
    )

    result = market_catalog_sync._sync_tw_futures_catalog()

    assert result == {"ok": False, "skipped": True, "rows": 0, "reason": "sidecar_not_configured"}


def test_twfutures_failure_leaves_twstock_and_crypto_untouched(monkeypatch):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "test-key")
    completed = {}
    written = []
    crypto_row = SymbolMasterRow("Crypto", "BTC/USDT", "Bitcoin", "binance", "USDT")
    stock_row = SymbolMasterRow("TWStock", "2330.TW", "台積電", "TWSE", "TWD", asset_class="equity")
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_crypto_symbols_with_diagnostics",
        lambda: ([crypto_row], [{"ok": True}]),
    )
    monkeypatch.setattr(market_catalog_sync, "reclassify_stored_equity_products", lambda: 0)
    monkeypatch.setattr(market_catalog_sync, "fetch_tw_stock_symbols", lambda: [stock_row])
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_futures_symbols",
        lambda: fetch_tw_futures_symbols(_UnavailableSource()),
    )
    monkeypatch.setattr(
        market_catalog_sync,
        "upsert_symbol_master",
        lambda rows: written.extend(rows) or len(rows),
    )
    monkeypatch.setattr(
        market_catalog_sync,
        "_finish_run",
        lambda run_id, status, result: completed.update(status=status, result=result),
    )

    market_catalog_sync._run_sync(9)

    assert completed["status"] == "success"
    assert completed["result"]["tw_stock"] == {"ok": True, "rows": 1, "upserted": 1}
    assert completed["result"]["tw_futures"]["skipped"] is True
    assert {row.market for row in written} == {"Crypto", "TWStock"}


def test_catalog_sync_upserts_twfutures_rows_when_sidecar_is_available(monkeypatch):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "test-key")
    written = []
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_futures_symbols",
        lambda: fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF),
    )
    monkeypatch.setattr(
        market_catalog_sync,
        "upsert_symbol_master",
        lambda rows: written.extend(rows) or len(rows),
    )

    result = market_catalog_sync._sync_tw_futures_catalog()

    assert result == {"ok": True, "rows": 9, "upserted": 9}
    assert {"TXF", "TXFK6"} <= {row.symbol for row in written}


def test_sync_symbol_master_isolates_a_twfutures_failure(monkeypatch):
    def unavailable():
        raise FubonSidecarError("Fubon market data sidecar is not ready (503 service unavailable)")

    stock_row = SymbolMasterRow("TWStock", "2330.TW", "台積電", "TWSE", "TWD", asset_class="equity")
    monkeypatch.setitem(symbol_master_sync.FETCHERS, "TWFutures", unavailable)
    monkeypatch.setitem(symbol_master_sync.FETCHERS, "TWStock", lambda: [stock_row])
    monkeypatch.setattr(symbol_master_sync, "upsert_symbol_master", lambda rows: len(rows))

    stats = sync_symbol_master(["TWFutures", "TWStock"])

    assert stats["TWFutures"]["ok"] is False
    assert "not ready" in stats["TWFutures"]["error"]
    assert stats["TWStock"] == {"ok": True, "rows": 1, "upserted": 1}
    assert FETCHERS["TWFutures"] is not None


# --------------------------------------------------------------------------- search fallback


class _DictCache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, _ttl=None):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def sidecar_search(monkeypatch):
    persisted = []
    monkeypatch.setattr(symbol_search, "_market_cache", _DictCache())
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda market, keyword, limit: [])
    monkeypatch.setattr(
        symbol_master_sync,
        "fetch_tw_futures_symbols",
        lambda: fetch_tw_futures_symbols(_Tickers(OCTOBER_LISTING), as_of=AS_OF),
    )
    monkeypatch.setattr(symbol_master_sync, "upsert_symbol_master", lambda rows: persisted.extend(rows))
    return persisted


def test_sidecar_search_by_product_prefix_lists_continuous_code_first(sidecar_search):
    results = symbol_search.search_market_symbols("TWFutures", "TX")

    assert [item["symbol"] for item in results] == ["TXF", "TXFK6", "TXFL6"]
    assert all(item["market"] == "TWFutures" for item in results)
    assert "aliases" not in results[0]
    assert sidecar_search, "sidecar catalog should be written to the local symbol master"


def test_sidecar_search_by_colloquial_chinese_name(sidecar_search):
    results = symbol_search.search_market_symbols("TWFutures", "台指")

    symbols = [item["symbol"] for item in results]
    assert symbols[:3] == ["TXF", "MXF", "TMF"]
    assert "TXFJ6" not in symbols
    assert results[0]["name"] == "臺股期貨"


def test_sidecar_search_by_contract_code(sidecar_search):
    results = symbol_search.search_market_symbols("TWFutures", "mxfk6")

    assert [item["symbol"] for item in results] == ["MXFK6"]


def test_sidecar_search_skips_expired_contracts(sidecar_search):
    assert symbol_search.search_market_symbols("TWFutures", "TXFJ6") == []


def test_sidecar_search_is_empty_when_sidecar_is_unavailable(monkeypatch):
    monkeypatch.setattr(symbol_search, "_market_cache", _DictCache())
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda market, keyword, limit: [])
    monkeypatch.setattr(
        symbol_master_sync,
        "fetch_tw_futures_symbols",
        lambda: fetch_tw_futures_symbols(_UnavailableSource()),
    )

    assert symbol_search.search_market_symbols("TWFutures", "TX") == []
