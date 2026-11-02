"""TAIFEX options: code parsing, catalog, chain endpoint and backtest refusal (#59).

The Fubon sidecar and PostgreSQL are never contacted: sources are faked.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import app.services.strategy_v2 as strategy_v2_pkg
from app.data_sources.tw_futures import parse_tw_futures_symbol
from app.data_sources.tw_options import (
    CALL,
    OPTIONS_UNSUPPORTED_ERROR,
    PUT,
    first_option_instrument,
    is_option_code,
    nth_wednesday,
    option_code,
    option_expiry_date,
    parse_option_symbol,
)
from app.data_sources.tw_stock import FubonSidecarError
from app.routes.agent_v1 import backtests
from app.services import symbol_master_sync, tw_kline_collector, tw_option_chain
from app.services.market import watchlist
from app.services.strategy_v2 import deployment as deployment_module
from app.services.strategy_v2 import service as strategy_service
from app.services.strategy_v2.contract import StrategyV2ContractError
from app.services.strategy_v2.models import (
    InstrumentSpec, StrategyManifest, SubscriptionSpec, UniverseSpec,
)
from app.services.tw_option_chain import (
    OptionChainError,
    OptionChainNotFound,
    TWOptionChainService,
    normalize_expiry_month,
)
from app.utils import agent_auth

TAIPEI = ZoneInfo("Asia/Taipei")
REFERENCE = date(2026, 11, 2)


def _weekdays(day: date) -> bool:
    return day.weekday() < 5


# --- code parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, right, year, month",
    [
        ("TXO23000H6", CALL, 2026, 8),   # H = August call
        ("TXO23000T6", PUT, 2026, 8),    # T = August put
        ("TXO18000A7", CALL, 2027, 1),   # A = January call
        ("TXO18000M7", PUT, 2027, 1),    # M = January put
        ("TXO25000L6", CALL, 2026, 12),  # L = December call
        ("TXO25000X6", PUT, 2026, 12),   # X = December put
    ],
)
def test_month_letters_encode_call_and_put(code, right, year, month):
    contract = parse_option_symbol(code, REFERENCE, _weekdays)
    assert contract is not None
    assert (contract.product, contract.right, contract.year, contract.month) == ("TXO", right, year, month)
    assert contract.strike == int(code[3:8])
    assert option_code("TXO", contract.strike, right, year, month) == code


def test_strike_is_zero_padded_to_five_digits():
    contract = parse_option_symbol("TXO09500K6", REFERENCE, _weekdays)
    assert contract.strike == 9500
    assert option_code("TXO", 9500, "c", 2026, 11) == "TXO09500K6"
    assert option_code("TXO", 9500, "P", 2026, 11) == "TXO09500W6"


@pytest.mark.parametrize(
    "code, reference, year",
    [
        ("TXO23000A7", date(2026, 11, 2), 2027),   # January series listed in November
        ("TXO23000M7", date(2026, 12, 20), 2027),
        ("TXO23000L6", date(2027, 1, 5), 2026),    # December series just after New Year
        ("TXO23000A0", date(2029, 12, 1), 2030),   # decade roll-over
    ],
)
def test_year_digit_resolves_across_year_ends(code, reference, year):
    assert parse_option_symbol(code, reference, _weekdays).year == year


def test_monthly_txo_expires_on_the_third_wednesday():
    assert parse_option_symbol("TXO23000H6", REFERENCE, _weekdays).expiry == date(2026, 8, 19)
    assert parse_option_symbol("TXO23000W6", REFERENCE, _weekdays).expiry == date(2026, 11, 18)
    assert parse_option_symbol("TXO23000W6", REFERENCE, _weekdays).expiry_month == "2026-11"


@pytest.mark.parametrize(
    "code, expiry",
    [
        ("TX123000H6", date(2026, 8, 5)),    # first Wednesday
        ("TX223000T6", date(2026, 8, 12)),   # second Wednesday (put)
        ("TX423000H6", date(2026, 8, 26)),   # fourth Wednesday
        ("TX523000L6", date(2026, 12, 30)),  # fifth Wednesday
    ],
)
def test_weekly_series_expire_on_their_wednesday(code, expiry):
    contract = parse_option_symbol(code, REFERENCE, _weekdays)
    assert contract.product == code[:3]
    assert contract.expiry == expiry


def test_weekly_expiry_moves_past_a_holiday():
    holiday = date(2026, 8, 5)
    expiry = option_expiry_date("TX1", 2026, 8, lambda d: d.weekday() < 5 and d != holiday)
    assert expiry == date(2026, 8, 6)


def test_fifth_week_series_needs_a_fifth_wednesday():
    assert nth_wednesday(2026, 11, 5) is None  # November 2026 has four Wednesdays
    assert parse_option_symbol("TX523000K6", REFERENCE, _weekdays) is None


def test_unknown_products_parse_without_a_calendar_expiry():
    contract = parse_option_symbol("TWFutures:CDO00600K6", REFERENCE, _weekdays)
    assert contract.product == "CDO"
    assert contract.expiry is None


@pytest.mark.parametrize(
    "code",
    [
        "", "TXF", "TXFH6",          # futures, not options
        "TXO2300H6",                 # four-digit strike
        "TXO230000H6",               # six-digit strike
        "TXO23000Y6",                # Y is not a month letter
        "TXO23000H",                 # missing year digit
        "TXO23000HH",
        "TX23000H6",                 # two-character product
        "1XO23000H6",                # product must start with a letter
        "TXO 23000H6",
        "TXO23000H6/T6",             # spread
    ],
)
def test_invalid_codes_are_rejected(code):
    assert parse_option_symbol(code, REFERENCE, _weekdays) is None
    assert is_option_code(code) is False


def test_option_code_rejects_bad_inputs():
    with pytest.raises(ValueError):
        option_code("TXO", 100000, CALL, 2026, 8)
    with pytest.raises(ValueError):
        option_code("TXO", 23000, "straddle", 2026, 8)
    with pytest.raises(ValueError):
        option_code("TXO", 23000, PUT, 2026, 13)


def test_is_option_code_accepts_prefix_and_session_suffix():
    assert is_option_code("TWFutures:TXO23000H6")
    assert is_option_code("txo23000t6@regular")


def test_first_option_instrument_only_matches_twfutures():
    assert first_option_instrument([{"market": "USStock", "symbol": "ABC12345D6"}]) is None
    assert first_option_instrument([
        {"market": "TWFutures", "symbol": "TXF"},
        {"market": "TWFutures", "symbol": "txo23000h6"},
    ]) == "TWFutures:TXO23000H6"


# --- provider / watchlist -------------------------------------------------------------


def test_twfutures_provider_reads_option_contracts():
    parsed = parse_tw_futures_symbol("TWFutures:TXO23000H6@regular")
    assert (parsed.product, parsed.contract, parsed.session) == ("TXO", "TXO23000H6", "regular")
    assert parsed.is_option is True
    assert parsed.continuous is False
    assert parse_tw_futures_symbol("TXFH6").is_option is False
    assert parse_tw_futures_symbol("TX123000T6").product == "TX1"


def test_watchlist_accepts_option_contracts():
    assert watchlist.validate_watchlist_pair("TWFutures", "TXO23000H6") is None
    assert watchlist.validate_watchlist_pair("TWFutures", "TX123000T6") is None
    assert watchlist.validate_watchlist_pair("TWFutures", "TXO23000Y6") is not None


# --- symbol catalog -----------------------------------------------------------------


class _Tickers:
    def __init__(self, option_error=None):
        self.option_error = option_error

    def get_futopt_tickers(self, ticker_type="FUTURE"):
        if ticker_type == "FUTURE":
            return [{"symbol": "TXFK6", "name": "臺股期貨115/11", "settlementDate": "2026-11-18"}]
        assert ticker_type == "OPTION"
        if self.option_error:
            raise self.option_error
        return [
            {"symbol": "TXO23000K6", "name": "臺指選擇權11月23000買權",
             "endDate": "2026-11-18", "settlementDate": "2026-11-18"},
            {"symbol": "TXO23000W6", "name": "臺指選擇權11月23000賣權", "settlementDate": "2026-11-18"},
            {"symbol": "TX123000L6", "name": "臺指選擇權W1"},
            {"symbol": "TXO23000K6/L6", "name": "價差"},
            {"symbol": "TXFK6", "name": "not an option"},
        ]


def test_option_catalog_rows_carry_strike_right_and_expiry():
    rows = symbol_master_sync.fetch_tw_option_symbols(_Tickers(), today=REFERENCE)
    by_symbol = {row.symbol: row for row in rows}
    assert set(by_symbol) == {"TXO23000K6", "TXO23000W6", "TX123000L6"}
    call = by_symbol["TXO23000K6"]
    assert (call.market, call.exchange, call.currency) == ("TWFutures", "TAIFEX", "TWD")
    assert (call.product_type, call.asset_class, call.api_family) == ("option", "option", "option")
    assert call.product_meta["strike"] == 23000
    assert call.product_meta["right"] == "call"
    assert call.product_meta["expiryMonth"] == "2026-11"
    assert call.product_meta["expiryDate"] == "2026-11-18"
    assert call.product_meta["backtestSupported"] is False
    assert call.product_meta["multiplier"] == 50
    assert by_symbol["TXO23000W6"].product_meta["right"] == "put"
    # Without ticker dates the TAIFEX calendar gives the first Wednesday.
    assert by_symbol["TX123000L6"].product_meta["expiryDate"] == "2026-12-02"


def test_twfutures_catalog_includes_options():
    rows = symbol_master_sync.fetch_tw_futures_symbols(_Tickers())
    types = {row.symbol: row.product_type for row in rows}
    assert types["TXF"] == "futures"
    assert types["TXFK6"] == "futures"
    assert types["TXO23000K6"] == "option"


def test_twfutures_catalog_keeps_futures_when_the_option_list_fails():
    rows = symbol_master_sync.fetch_tw_futures_symbols(_Tickers(option_error=FubonSidecarError("down")))
    assert {row.symbol for row in rows} == {"TXF", "TXFK6"}


def test_upsert_deactivates_only_the_synced_product_types(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, sql, params=()):
            executed.append((" ".join(sql.split()), params))

        def close(self):
            pass

    class Db:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    @contextmanager
    def fake_db():
        yield Db()

    monkeypatch.setattr(symbol_master_sync, "get_db_connection", fake_db)
    futures_only = symbol_master_sync.fetch_tw_futures_symbols(_Tickers(option_error=FubonSidecarError("down")))
    symbol_master_sync.upsert_symbol_master(futures_only)
    deactivations = [params for sql, params in executed if sql.startswith("UPDATE qd_market_symbols SET is_active = 0")]
    assert deactivations == [("futures",)]

    executed.clear()
    symbol_master_sync.upsert_symbol_master(symbol_master_sync.fetch_tw_futures_symbols(_Tickers()))
    deactivations = [params for sql, params in executed if sql.startswith("UPDATE qd_market_symbols SET is_active = 0")]
    assert deactivations == [("futures",), ("option",)]


# --- daily K-line accumulation ------------------------------------------------------------


def test_collector_takes_options_only_from_the_configured_list():
    class Repo:
        def watchlist_symbols(self):
            return [("TWFutures", "TXO24000K6"), ("TWFutures", "TXF")]

    collector = tw_kline_collector.TWKlineCollector(source=object(), repository=Repo())
    targets = collector.resolve_targets([
        ("TWFutures", "MXF"),
        ("TWFutures", "TXO23000K6"),
        ("TWFutures", "TXO"),  # an option product is never expanded into every strike
    ])
    assert targets.futures_products == ["MXF", "TXF"]
    assert targets.futures_contracts == ["TXO23000K6"]


def test_default_collection_list_has_no_options():
    entries = tw_kline_collector.configured_symbol_entries(tw_kline_collector.DEFAULT_COLLECT_SYMBOLS)
    targets = tw_kline_collector.parse_collect_symbols(entries)
    assert not any(is_option_code(symbol) for symbol in targets.futures_contracts)


# --- option chain -------------------------------------------------------------------


class _ChainSource:
    """Fake sidecar: OPTION tickers, per-contract quotes and the TAIEX quote."""

    def __init__(self, strikes=(22800, 22900, 23000, 23100, 23200), taiex=23040.0, tickers_error=None):
        self.strikes = strikes
        self.taiex = taiex
        self.tickers_error = tickers_error
        self.ticker_calls = 0
        self.quoted = []

    def get_futopt_tickers(self, ticker_type="FUTURE"):
        assert ticker_type == "OPTION"
        self.ticker_calls += 1
        if self.tickers_error:
            raise self.tickers_error
        rows = []
        for strike in self.strikes:
            rows.append({"symbol": f"TXO{strike:05d}K6", "settlementDate": "2026-11-18"})
            rows.append({"symbol": f"TXO{strike:05d}W6", "settlementDate": "2026-11-18"})
        rows.append({"symbol": "TXO23000L6", "settlementDate": "2026-12-16"})
        rows.append({"symbol": "TX123000L6"})
        rows.append({"symbol": "TXO23000J6", "settlementDate": "2026-10-21"})  # expired
        return rows

    def _request(self, path, params):
        symbol = params["symbol"]
        if path == "/v1/stock/quote":
            assert symbol == "IX0001"
            return {"symbol": symbol, "lastPrice": self.taiex} if self.taiex else None
        assert path == "/v1/futopt/quote"
        self.quoted.append(symbol)
        if symbol == "TXO23200W6":
            return None  # sidecar 404: no quote
        if symbol == "TXO22800K6":
            raise FubonSidecarError("rate limited")
        strike = int(symbol[3:8])
        return {"symbol": symbol, "name": symbol, "lastPrice": float(strike % 1000 + 10),
                "previousClose": 100.0, "volume": 12.0, "time": 1_793_000_000}


def _chain_service(source, now="2026-11-02T10:00"):
    clock = datetime.fromisoformat(now).replace(tzinfo=TAIPEI).timestamp()
    return TWOptionChainService(source, clock=lambda: clock, quote_workers=1)


def test_chain_defaults_to_the_nearest_unexpired_month_centred_on_taiex():
    source = _ChainSource()
    chain = _chain_service(source).get_chain("txo", strikes=3)

    assert chain["product"] == "TXO"
    assert chain["expiryMonth"] == "2026-11"
    assert chain["expiryDate"] == "2026-11-18"
    assert chain["underlying"] == {"symbol": "TWStock:IX0001", "price": 23040.0}
    assert [row["strike"] for row in chain["strikes"]] == [22900, 23000, 23100]
    assert chain["strikeCount"] == 5
    assert chain["truncated"] is True
    assert chain["backtestSupported"] is False
    row = chain["strikes"][1]
    assert row["call"]["symbol"] == "TXO23000K6"
    assert row["put"]["symbol"] == "TXO23000W6"
    assert row["call"]["available"] is True
    assert row["call"]["lastPrice"] == 10.0
    assert sorted(source.quoted) == sorted(
        f"TXO{strike}{letter}6" for strike in (22900, 23000, 23100) for letter in ("K", "W")
    )


def test_chain_marks_missing_quotes_unavailable():
    source = _ChainSource()
    chain = _chain_service(source).get_chain("TXO", "2026-11", strikes=25, center=23000)
    rows = {row["strike"]: row for row in chain["strikes"]}
    assert len(rows) == 5 and chain["truncated"] is False
    assert rows[23200]["put"] == {"symbol": "TXO23200W6", "available": False}
    assert rows[22800]["call"] == {"symbol": "TXO22800K6", "available": False}
    assert rows[22800]["put"]["available"] is True


def test_chain_selects_the_requested_month_and_weekly_product():
    source = _ChainSource()
    december = _chain_service(source).get_chain("TXO", "202612")
    assert [row["strike"] for row in december["strikes"]] == [23000]
    assert december["strikes"][0]["put"] is None
    weekly = _chain_service(source).get_chain("TX1", "2026-12")
    assert weekly["productName"] == "TAIEX Weekly Options (Week 1)"
    assert weekly["expiryDate"] == "2026-12-02"  # calendar first Wednesday, no ticker dates


def test_chain_without_taiex_centres_on_the_middle_strike():
    chain = _chain_service(_ChainSource(taiex=None)).get_chain("TXO", strikes=1)
    assert [row["strike"] for row in chain["strikes"]] == [23000]
    assert chain["underlying"]["price"] is None


def test_chain_filters_contracts_by_the_instrument_allowlist():
    allowed = {"TXO23000K6"}
    chain = _chain_service(_ChainSource()).get_chain("TXO", symbol_allowed=lambda s: s in allowed)
    assert [row["strike"] for row in chain["strikes"]] == [23000]
    assert chain["strikes"][0]["put"] is None
    with pytest.raises(OptionChainNotFound):
        _chain_service(_ChainSource()).get_chain("TXO", symbol_allowed=lambda s: False)


def test_chain_caches_the_ticker_list():
    source = _ChainSource()
    service = _chain_service(source)
    service.get_chain("TXO", strikes=1)
    service.get_chain("TXO", strikes=1)
    assert source.ticker_calls == 1


@pytest.mark.parametrize("month", ["2026-13", "26-11", "abc"])
def test_chain_rejects_malformed_months(month):
    with pytest.raises(OptionChainError):
        normalize_expiry_month(month)


def test_chain_reports_unlisted_products_and_months():
    with pytest.raises(OptionChainNotFound):
        _chain_service(_ChainSource()).get_chain("TX5")
    with pytest.raises(OptionChainNotFound):
        _chain_service(_ChainSource()).get_chain("TXO", "2027-03")


# --- agent gateway ------------------------------------------------------------------------


_HEADERS = {"Authorization": "Bearer qd_agent_TWOPTIONTEST"}


@pytest.fixture
def option_token(monkeypatch):
    token = {"id": 9, "user_id": 5, "scopes": "R", "markets": "TWFutures",
             "instruments": "*", "status": "active", "rate_limit_per_min": 100,
             "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    yield token
    agent_auth._rate_state.clear()


@pytest.fixture
def chain_source(monkeypatch):
    source = _ChainSource()
    service = _chain_service(source)
    monkeypatch.setattr(tw_option_chain, "get_tw_option_chain_service", lambda: service)
    return source


def test_agent_option_chain_endpoint(client, option_token, chain_source):
    response = client.get(
        "/api/agent/v1/option-chain",
        query_string={"product": "TXO", "expiry_month": "2026-11", "strikes": 2, "center": 23000},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["expiryMonth"] == "2026-11"
    assert [row["strike"] for row in data["strikes"]] == [22900, 23000]
    assert data["strikes"][1]["put"]["symbol"] == "TXO23000W6"


def test_agent_option_chain_is_read_scope_only(client, option_token, chain_source):
    for method in (client.post, client.put, client.delete):
        assert method("/api/agent/v1/option-chain", headers=_HEADERS).status_code == 405


def test_agent_option_chain_honours_the_instrument_allowlist(client, option_token, chain_source):
    option_token["instruments"] = "TXO23000K6"
    data = client.get(
        "/api/agent/v1/option-chain", query_string={"product": "TXO"}, headers=_HEADERS,
    ).get_json()["data"]
    assert [row["strike"] for row in data["strikes"]] == [23000]
    assert data["strikes"][0]["put"] is None

    option_token["instruments"] = "2330.TW"
    response = client.get("/api/agent/v1/option-chain", query_string={"product": "TXO"}, headers=_HEADERS)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "query, status",
    [
        ({}, 400),
        ({"product": "TXO", "expiry_month": "2026/13"}, 400),
        ({"product": "TXO", "center": "abc"}, 400),
        ({"product": "TXO", "center": "-5"}, 400),
        ({"product": "TX5"}, 404),
    ],
)
def test_agent_option_chain_rejects_bad_requests(client, option_token, chain_source, query, status):
    response = client.get("/api/agent/v1/option-chain", query_string=query, headers=_HEADERS)
    assert response.status_code == status


def test_agent_option_chain_requires_the_twfutures_market(client, option_token, chain_source):
    option_token["markets"] = "TWStock"
    response = client.get("/api/agent/v1/option-chain", query_string={"product": "TXO"}, headers=_HEADERS)
    assert response.status_code == 403


def test_agent_option_chain_reports_sidecar_outage(client, option_token, monkeypatch):
    service = _chain_service(_ChainSource(tickers_error=FubonSidecarError("sidecar unreachable")))
    monkeypatch.setattr(tw_option_chain, "get_tw_option_chain_service", lambda: service)
    response = client.get("/api/agent/v1/option-chain", query_string={"product": "TXO"}, headers=_HEADERS)
    assert response.status_code == 503


# --- backtests and deployments are refused -------------------------------------------------


def _option_manifest(symbol: str = "TXO23000K6") -> StrategyManifest:
    instrument = InstrumentSpec(market="TWFutures", symbol=symbol)
    return StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cta",
        universe=UniverseSpec(kind="static", instruments=(instrument,)),
        subscriptions=(SubscriptionSpec(instruments=(instrument,), frequency="15m"),),
        schedules=(),
        handlers=("handle_data",),
    )


@pytest.fixture
def backtest_submission(monkeypatch):
    token = {"id": 7, "user_id": 5, "scopes": "R,B", "markets": "*", "instruments": "*",
             "status": "active", "rate_limit_per_min": 100, "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(agent_auth, "_reserve_idempotency", lambda *_: ("reserved", None))
    monkeypatch.setattr(agent_auth, "_complete_idempotency", lambda *_: None)
    monkeypatch.setattr(backtests, "count_active_jobs", lambda **kw: 0)
    submit = Mock(return_value={"job_id": "job-1", "status": "queued"})
    monkeypatch.setattr(backtests, "submit_job", submit)
    yield submit
    agent_auth._rate_state.clear()


def test_agent_backtest_submission_rejects_options(client, backtest_submission, monkeypatch):
    manifest = _option_manifest()
    monkeypatch.setattr(strategy_v2_pkg, "compile_strategy_v2", lambda _code: SimpleNamespace(manifest=manifest))
    response = client.post(
        "/api/agent/v1/backtest/run",
        json={"code": "compiled-by-fixture", "startDate": "2026-10-01", "endDate": "2026-10-31"},
        headers={"Authorization": "Bearer qd_agent_OPTIONBTTEST", "Idempotency-Key": "test-key"},
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["message"] == OPTIONS_UNSUPPORTED_ERROR == "strategyV2.optionsBacktestUnsupported"
    assert body["details"]["instrument"] == "TWFutures:TXO23000K6"
    backtest_submission.assert_not_called()


def test_backtest_service_rejects_options_before_loading_data(monkeypatch):
    manifest = _option_manifest("TX123000T6")
    monkeypatch.setattr(strategy_service, "compile_strategy_v2", lambda _code: SimpleNamespace(manifest=manifest))
    universe = Mock()
    service = strategy_service.StrategyV2BacktestService(
        repository=Mock(), universe_service=universe, frame_fetcher=Mock(side_effect=AssertionError("fetched")),
    )
    with pytest.raises(StrategyV2ContractError, match="strategyV2.optionsBacktestUnsupported"):
        service.run(
            user_id=5, code="compiled-by-fixture",
            start_date=datetime(2026, 10, 1), end_date=datetime(2026, 10, 31), initial_capital=100000,
        )


def test_backtest_service_rejects_options_from_a_dynamic_universe(monkeypatch):
    with pytest.raises(StrategyV2ContractError, match="strategyV2.optionsBacktestUnsupported"):
        strategy_service.reject_tw_options([
            {"market": "TWFutures", "symbol": "TXF"},
            {"market": "TWFutures", "symbol": "TXO23000K6"},
        ])
    strategy_service.reject_tw_options([{"market": "TWFutures", "symbol": "TXFK6"}])


def test_paper_deployment_rejects_options(monkeypatch):
    sources = Mock()
    sources.get_source.return_value = {"id": 3, "name": "options"}
    sources.get_latest_version.return_value = {"id": 4, "code": "compiled-by-fixture"}
    monkeypatch.setattr(deployment_module, "get_script_source_service", lambda: sources)
    manifest = _option_manifest()
    monkeypatch.setattr(deployment_module, "compile_strategy_v2", lambda _code: SimpleNamespace(manifest=manifest))

    with pytest.raises(StrategyV2ContractError, match="strategyV2.optionsBacktestUnsupported"):
        deployment_module.StrategyV2DeploymentService().save(
            user_id=5,
            payload={"sourceId": 3, "name": "options", "initialCapital": 100000, "executionMode": "signal"},
        )
