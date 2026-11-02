"""TWStock symbol master sync, search fallback and name resolution (issue #55)."""

from __future__ import annotations

import pytest

from app.data_sources.tw_stock import FubonSidecarError, TWStockDataSource
from app.services import market_catalog_sync, symbol_master_sync
from app.services.market import symbol_search
from app.services.symbol_master_sync import (
    SymbolMasterRow,
    classify_tw_ticker,
    fetch_tw_stock_symbols,
    sync_symbol_master,
)


SIDECAR_TICKERS = {
    ("EQUITY", "TWSE"): [
        {"symbol": "2330", "name": "台積電"},
        {"symbol": "0050", "name": "元大台灣50"},
        {"symbol": "006208", "name": "富邦台50"},
        {"symbol": "00631L", "name": "元大台灣50正2"},
        {"symbol": "00679B", "name": "元大美債20年"},
        {"symbol": "2881A", "name": "富邦特"},  # preferred share
        {"symbol": "23301", "name": "台積電一"},  # convertible bond
        {"symbol": "020000", "name": "富邦特選蘋果N"},  # ETN
        {"symbol": "030001", "name": "台積電元大5A購01"},  # warrant
        {"symbol": "", "name": "blank"},
        {"symbol": "1101", "name": ""},
    ],
    ("EQUITY", "TPEx"): [
        {"symbol": "6488", "name": "環球晶"},
        {"symbol": "00679B", "name": "元大美債20年"},
        {"symbol": "70000P", "name": "某權證"},
    ],
    ("INDEX", "TWSE"): [
        {"symbol": "IX0001", "name": "發行量加權股價指數"},
        {"symbol": "IX0010", "name": "水泥類指數"},
    ],
    ("INDEX", "TPEx"): [
        {"symbol": "IX0043", "name": "櫃買指數"},
    ],
}


class _FakeTickerSource:
    def __init__(self, tickers=None):
        self.tickers = SIDECAR_TICKERS if tickers is None else tickers
        self.calls = []

    def get_tickers(self, ticker_type, exchange=None):
        self.calls.append((ticker_type, exchange))
        return list(self.tickers.get((ticker_type, exchange), []))


class _UnavailableSource:
    def get_tickers(self, ticker_type, exchange=None):
        raise FubonSidecarError("Fubon market data sidecar at http://sidecar is unreachable")


def _by_symbol(rows):
    return {row.symbol: row for row in rows}


# --------------------------------------------------------------------------- mapping


def test_listed_otc_and_index_codes_map_to_twstock_symbols():
    rows = _by_symbol(fetch_tw_stock_symbols(_FakeTickerSource()))

    assert rows["2330.TW"] == SymbolMasterRow("TWStock", "2330.TW", "台積電", "TWSE", "TWD", asset_class="equity")
    assert rows["6488.TWO"] == SymbolMasterRow("TWStock", "6488.TWO", "環球晶", "TPEx", "TWD", asset_class="equity")
    assert rows["IX0001"] == SymbolMasterRow("TWStock", "IX0001", "發行量加權股價指數", "TWSE", "TWD", asset_class="index")
    assert rows["IX0043"].exchange == "TPEx"
    assert rows["IX0043"].asset_class == "index"


def test_etfs_are_classified_separately_from_stocks():
    rows = _by_symbol(fetch_tw_stock_symbols(_FakeTickerSource()))

    for symbol in ("0050.TW", "006208.TW", "00631L.TW", "00679B.TW", "00679B.TWO"):
        assert rows[symbol].asset_class == "etf"
        assert rows[symbol].market == "TWStock"
        assert rows[symbol].currency == "TWD"


def test_warrants_bonds_etns_preferred_and_minor_indices_are_excluded():
    symbols = set(_by_symbol(fetch_tw_stock_symbols(_FakeTickerSource())))

    for code in ("2881A", "23301", "020000", "030001", "70000P"):
        assert not any(symbol.split(".")[0] == code for symbol in symbols), code
    assert "IX0010" not in symbols
    assert "1101.TW" not in symbols  # no name


def test_only_equity_and_index_ticker_types_are_requested():
    source = _FakeTickerSource()

    fetch_tw_stock_symbols(source)

    assert source.calls == [
        ("EQUITY", "TWSE"),
        ("EQUITY", "TPEx"),
        ("INDEX", "TWSE"),
        ("INDEX", "TPEx"),
    ]


def test_classifier_rejects_warrant_ticker_type_and_unknown_exchange():
    assert classify_tw_ticker("WARRANT", "TWSE", {"symbol": "2330", "name": "台積電"}) is None
    assert classify_tw_ticker("EQUITY", "NYSE", {"symbol": "2330", "name": "台積電"}) is None


def test_repeated_fetch_returns_identical_rows():
    first = fetch_tw_stock_symbols(_FakeTickerSource())
    second = fetch_tw_stock_symbols(_FakeTickerSource())

    assert first == second
    assert len({(row.symbol, row.exchange) for row in first}) == len(first)


# --------------------------------------------------------------------------- upsert idempotency


class _RecordingCursor:
    def __init__(self, log):
        self.log = log
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), tuple(params)))

    def close(self):
        return None


class _RecordingConnection:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return _RecordingCursor(self.log)

    def commit(self):
        return None


def test_upsert_is_an_idempotent_conflict_update_that_keeps_hot_flags(monkeypatch):
    log = []
    monkeypatch.setattr(symbol_master_sync, "get_db_connection", lambda: _RecordingConnection(log))
    rows = fetch_tw_stock_symbols(_FakeTickerSource())

    symbol_master_sync.upsert_symbol_master(rows)
    first = list(log)
    log.clear()
    symbol_master_sync.upsert_symbol_master(fetch_tw_stock_symbols(_FakeTickerSource()))

    assert first == log
    assert len(first) == len(rows)
    for sql, _params in first:
        assert sql.startswith("INSERT INTO qd_market_symbols")
        assert "ON CONFLICT (market, symbol, exchange, market_type, instrument_id) DO UPDATE" in sql
        # curated hot flags and sort order from the seed must survive a sync
        assert "is_hot =" not in sql.split("DO UPDATE", 1)[1]
        assert "sort_order =" not in sql.split("DO UPDATE", 1)[1]
    asset_classes = {params[1]: params[8] for _sql, params in first}
    assert asset_classes["2330.TW"] == "equity"
    assert asset_classes["0050.TW"] == "etf"
    assert asset_classes["IX0001"] == "index"


# --------------------------------------------------------------------------- unavailable sidecar


def test_sync_symbol_master_reports_unavailable_sidecar_without_raising(monkeypatch):
    def unavailable():
        raise FubonSidecarError("Fubon market data sidecar is not ready (503 service unavailable)")

    monkeypatch.setitem(symbol_master_sync.FETCHERS, "TWStock", unavailable)

    stats = sync_symbol_master(["TWStock"])

    assert stats["TWStock"]["ok"] is False
    assert "not ready" in stats["TWStock"]["error"]


def test_catalog_sync_skips_twstock_when_sidecar_is_not_configured(monkeypatch):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "")
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_stock_symbols",
        lambda: (_ for _ in ()).throw(AssertionError("sidecar should not be called")),
    )

    result = market_catalog_sync._sync_tw_stock_catalog()

    assert result == {"ok": False, "skipped": True, "rows": 0, "reason": "sidecar_not_configured"}


def test_catalog_sync_skips_unreachable_sidecar_and_keeps_crypto_status(monkeypatch):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "test-key")
    completed = {}
    monkeypatch.setattr(market_catalog_sync, "fetch_crypto_symbols_with_diagnostics", lambda: ([], []))
    monkeypatch.setattr(market_catalog_sync, "reclassify_stored_equity_products", lambda: 0)
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_stock_symbols",
        lambda: fetch_tw_stock_symbols(_UnavailableSource()),
    )
    monkeypatch.setattr(
        market_catalog_sync,
        "_finish_run",
        lambda run_id, status, result: completed.update(status=status, result=result),
    )

    market_catalog_sync._run_sync(7)

    assert completed["status"] == "success"
    tw_stock = completed["result"]["tw_stock"]
    assert tw_stock["ok"] is False
    assert tw_stock["skipped"] is True
    assert "unreachable" in tw_stock["error"]


def test_catalog_sync_upserts_twstock_rows_when_sidecar_is_available(monkeypatch):
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "test-key")
    written = []
    monkeypatch.setattr(
        market_catalog_sync,
        "fetch_tw_stock_symbols",
        lambda: fetch_tw_stock_symbols(_FakeTickerSource()),
    )
    monkeypatch.setattr(
        market_catalog_sync,
        "upsert_symbol_master",
        lambda rows: written.extend(rows) or len(rows),
    )

    result = market_catalog_sync._sync_tw_stock_catalog()

    assert result["ok"] is True
    assert result["rows"] == result["upserted"] == len(written)
    assert {row.market for row in written} == {"TWStock"}


# --------------------------------------------------------------------------- data source


def test_data_source_get_tickers_reads_sidecar_ticker_list(monkeypatch):
    source = TWStockDataSource()
    calls = []

    def fake_request(path, params):
        calls.append((path, params))
        return {"type": "EQUITY", "exchange": "TPEx", "tickers": [{"symbol": "6488", "name": "環球晶"}, "bad"]}

    monkeypatch.setattr(source, "_request", fake_request)

    assert source.get_tickers("EQUITY", "TPEx") == [{"symbol": "6488", "name": "環球晶"}]
    assert calls == [("/v1/stock/tickers", {"type": "EQUITY", "exchange": "TPEx"})]


def test_data_source_get_tickers_treats_not_found_as_empty(monkeypatch):
    source = TWStockDataSource()
    monkeypatch.setattr(source, "_request", lambda path, params: None)

    assert source.get_tickers("INDEX", "TPEx") == []


# --------------------------------------------------------------------------- search fallback


@pytest.fixture
def clean_tw_cache():
    symbol_search._market_cache.delete(symbol_search.TW_STOCK_CATALOG_CACHE_KEY)
    yield
    symbol_search._market_cache.delete(symbol_search.TW_STOCK_CATALOG_CACHE_KEY)


def test_search_uses_local_catalog_without_calling_sidecar(monkeypatch, clean_tw_cache):
    monkeypatch.setattr(
        symbol_search,
        "seed_search_symbols",
        lambda market, keyword, limit: [{"market": "TWStock", "symbol": "2330.TW", "name": "台積電"}],
    )
    monkeypatch.setattr(
        symbol_search,
        "_search_tw_sidecar",
        lambda keyword, limit: (_ for _ in ()).throw(AssertionError("sidecar should not be called")),
    )

    results = symbol_search.search_market_symbols("TWStock", "台積")

    assert [item["symbol"] for item in results] == ["2330.TW"]


def test_search_falls_back_to_sidecar_only_when_local_catalog_misses(monkeypatch, clean_tw_cache):
    persisted = []
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda market, keyword, limit: [])
    monkeypatch.setattr(
        symbol_master_sync,
        "fetch_tw_stock_symbols",
        lambda: fetch_tw_stock_symbols(_FakeTickerSource()),
    )
    monkeypatch.setattr(symbol_master_sync, "upsert_symbol_master", lambda rows: persisted.extend(rows))

    by_name = symbol_search.search_market_symbols("TWStock", "環球")
    by_code = symbol_search.search_market_symbols("TWStock", "6488")

    assert [item["symbol"] for item in by_name] == ["6488.TWO"]
    assert [item["symbol"] for item in by_code] == ["6488.TWO"]
    assert by_name[0]["name"] == "環球晶"
    assert by_name[0]["asset_class"] == "equity"
    assert persisted, "sidecar catalog should be written to the local symbol master"


def test_find_market_symbol_uses_sidecar_fallback_for_twstock(monkeypatch, clean_tw_cache):
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda market, keyword, limit: [])
    monkeypatch.setattr(
        symbol_master_sync,
        "fetch_tw_stock_symbols",
        lambda: fetch_tw_stock_symbols(_FakeTickerSource()),
    )
    monkeypatch.setattr(symbol_master_sync, "upsert_symbol_master", lambda rows: len(rows))

    matched = symbol_search.find_market_symbol("TWStock", "IX0043")

    assert matched["symbol"] == "IX0043"
    assert matched["name"] == "櫃買指數"


def test_search_returns_empty_when_sidecar_is_unavailable(monkeypatch, clean_tw_cache):
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda market, keyword, limit: [])
    monkeypatch.setattr(
        symbol_master_sync,
        "fetch_tw_stock_symbols",
        lambda: fetch_tw_stock_symbols(_UnavailableSource()),
    )

    assert symbol_search.search_market_symbols("TWStock", "台積") == []


# --------------------------------------------------------------------------- name resolution


class _NameCursor:
    def __init__(self, names, queries):
        self.names = names
        self.queries = queries
        self._row = None

    def execute(self, _sql, params):
        self.queries.append(params)
        market, symbol = params
        name = self.names.get((market, symbol))
        self._row = {"name": name} if name else None

    def fetchone(self):
        return self._row

    def close(self):
        return None


class _NameConnection:
    def __init__(self, names, queries):
        self.names = names
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return _NameCursor(self.names, self.queries)


def test_symbol_name_resolves_from_symbol_master_including_bare_codes(monkeypatch):
    from app.data import market_symbols_seed
    from app.services import symbol_name

    names = {("TWStock", "2330.TW"): "台積電", ("TWStock", "6488.TWO"): "環球晶"}
    queries = []
    monkeypatch.setattr(market_symbols_seed, "_get_db_connection", lambda: _NameConnection(names, queries))
    monkeypatch.setattr(
        TWStockDataSource,
        "get_ticker",
        lambda self, symbol: (_ for _ in ()).throw(AssertionError("sidecar quote should not be called")),
    )

    assert symbol_name.resolve_symbol_name("TWStock", "2330.TW") == "台積電"
    assert symbol_name.resolve_symbol_name("TWStock", "TWStock:2330.TW") == "台積電"
    assert symbol_name.resolve_symbol_name("TWStock", "6488") == "環球晶"
    assert ("TWStock", "6488.TWO") in queries
