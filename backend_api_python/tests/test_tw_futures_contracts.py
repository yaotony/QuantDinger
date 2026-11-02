"""TAIFEX contract codes, expiry and the near-month rule (issue #57)."""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from app.data_sources.tw_futures_contracts import (
    CONTRACT_SPECS,
    ContractMonth,
    contract_code,
    expiry_date,
    front_month,
    front_month_segments,
    is_expired,
    month_code,
    near_month_contracts,
    next_trading_day,
    parse_contract,
    third_wednesday,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _tp(day: str, clock: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), tzinfo=TAIPEI)


def weekdays(*holidays: str):
    closed = {date.fromisoformat(item) for item in holidays}
    return lambda day: day.weekday() < 5 and day not in closed


# --- month letters ------------------------------------------------------------------


@pytest.mark.parametrize("month, letter", [(1, "A"), (2, "B"), (8, "H"), (11, "K"), (12, "L")])
def test_month_letters_run_a_to_l(month, letter):
    assert month_code(month) == letter


def test_month_code_rejects_out_of_range():
    with pytest.raises(ValueError):
        month_code(13)


def test_contract_code_uses_last_year_digit():
    assert contract_code("TXF", 2026, 8) == "TXFH6"
    assert contract_code("mxf", 2030, 1) == "MXFA0"


def test_parse_contract_picks_the_nearest_decade():
    assert parse_contract("TXFH6", date(2026, 8, 1)) == ContractMonth("TXF", 2026, 8)
    # Late in the year the next January contract belongs to the next year (L -> A).
    assert parse_contract("TXFA7", date(2026, 12, 20)) == ContractMonth("TXF", 2027, 1)
    # Early in the year the previous December contract still decodes to last year.
    assert parse_contract("TXFL6", date(2027, 1, 5)) == ContractMonth("TXF", 2026, 12)
    assert parse_contract("TXFA0", date(2029, 12, 1)) == ContractMonth("TXF", 2030, 1)


@pytest.mark.parametrize("symbol", ["TXF", "TXFM6", "TXFK6/L6", "TXO18000K6", ""])
def test_parse_contract_rejects_non_monthly_codes(symbol):
    assert parse_contract(symbol, date(2026, 1, 1)) is None


# --- expiry -------------------------------------------------------------------------


def test_third_wednesday():
    assert third_wednesday(2026, 11) == date(2026, 11, 18)
    assert third_wednesday(2026, 12) == date(2026, 12, 16)
    assert third_wednesday(2027, 1) == date(2027, 1, 20)


def test_expiry_moves_to_next_trading_day_after_a_holiday():
    assert expiry_date(2026, 11, weekdays()) == date(2026, 11, 18)
    assert expiry_date(2026, 11, weekdays("2026-11-18")) == date(2026, 11, 19)


def test_contract_trades_until_1330_on_its_last_day():
    expiry = date(2026, 11, 18)
    assert is_expired(expiry, _tp("2026-11-18", "13:29")) is False
    assert is_expired(expiry, _tp("2026-11-18", "13:30")) is True
    assert is_expired(expiry, _tp("2026-11-17", "23:00")) is False
    # Naive datetimes are Asia/Taipei wall time.
    assert is_expired(expiry, datetime(2026, 11, 18, 13, 30)) is True
    # UTC instants are converted: 05:29 UTC == 13:29 Taipei.
    assert is_expired(expiry, datetime(2026, 11, 18, 5, 29, tzinfo=ZoneInfo("UTC"))) is False
    # A plain date is the start of that trading day.
    assert is_expired(expiry, date(2026, 11, 18)) is False
    assert is_expired(expiry, date(2026, 11, 19)) is True


def test_next_trading_day_skips_weekends_and_holidays():
    assert next_trading_day(date(2026, 11, 20), weekdays()) == date(2026, 11, 23)
    assert next_trading_day(date(2026, 11, 20), weekdays("2026-11-23")) == date(2026, 11, 24)


# --- near month (calendar) ------------------------------------------------------------


def test_front_month_rolls_at_1330_on_expiry_day():
    cal = weekdays()
    assert front_month("TXF", _tp("2026-11-18", "13:29"), cal).code == "TXFK6"
    assert front_month("TXF", _tp("2026-11-18", "13:30"), cal).code == "TXFL6"
    # The night session that evening trades the next month.
    assert front_month("TXF", _tp("2026-11-18", "15:00"), cal).code == "TXFL6"
    assert front_month("TXF", _tp("2026-11-02", "09:00"), cal).code == "TXFK6"


def test_front_month_crosses_the_year_from_l_to_a():
    cal = weekdays()
    assert front_month("TXF", _tp("2026-12-16", "13:29"), cal).code == "TXFL6"
    assert front_month("TXF", _tp("2026-12-16", "13:30"), cal).code == "TXFA7"
    assert front_month("MXF", _tp("2026-12-31", "10:00"), cal).code == "MXFA7"


def test_front_month_follows_a_holiday_shifted_expiry():
    cal = weekdays("2026-11-18")
    assert front_month("TXF", _tp("2026-11-18", "15:00"), cal).code == "TXFK6"
    assert front_month("TXF", _tp("2026-11-19", "13:30"), cal).code == "TXFL6"


def test_front_month_segments_split_at_the_roll():
    cal = weekdays()
    start = int(_tp("2026-11-17", "00:00").timestamp())
    roll = int(_tp("2026-11-18", "13:30").timestamp())
    end = int(_tp("2026-12-17", "00:00").timestamp())
    roll_dec = int(_tp("2026-12-16", "13:30").timestamp())
    assert front_month_segments("TXF", start, end, cal) == [
        ("TXFK6", start, roll),
        ("TXFL6", roll, roll_dec),
        ("TXFA7", roll_dec, end),
    ]


def test_front_month_segments_of_an_empty_window():
    assert front_month_segments("TXF", 100, 100, weekdays()) == []


# --- near month (sidecar tickers) -----------------------------------------------------


TICKERS = [
    {"symbol": "TXFK6", "settlementDate": "2026-11-18", "endDate": "2026-11-18"},
    {"symbol": "TXFL6", "settlementDate": "2026-12-16", "endDate": "2026-12-16"},
    {"symbol": "TXFA7", "settlementDate": "2027-01-20", "endDate": "2027-01-20"},
    {"symbol": "TXFK6/L6", "settlementDate": "2026-11-18"},
    {"symbol": "TXO18000K6", "settlementDate": "2026-11-18"},
    {"symbol": "MXFK6", "settlementDate": "2026-11-18"},
]


def test_tickers_near_month_uses_the_same_1330_rule():
    assert near_month_contracts(TICKERS, "TXF", _tp("2026-11-18", "13:29")) == ["TXFK6", "TXFL6"]
    assert near_month_contracts(TICKERS, "TXF", _tp("2026-11-18", "13:30")) == ["TXFL6", "TXFA7"]
    assert near_month_contracts(TICKERS, "TXF", _tp("2026-11-18", "13:30"), count=1) == ["TXFL6"]


def test_tickers_near_month_expires_on_end_date_and_orders_by_settlement():
    tickers = [
        {"symbol": "TXFK6", "settlementDate": "2026-11-19", "endDate": "2026-11-18"},
        {"symbol": "TXFL6", "settlementDate": "2026-12-16"},
    ]
    assert near_month_contracts(tickers, "TXF", _tp("2026-11-18", "14:00")) == ["TXFL6"]


def test_tickers_near_month_ignores_spreads_options_and_other_products():
    assert near_month_contracts(TICKERS, "TXO", _tp("2026-11-02", "09:00")) == []
    assert near_month_contracts(TICKERS, "MXF", _tp("2026-11-02", "09:00")) == ["MXFK6"]


# --- contract terms -------------------------------------------------------------------


def test_contract_multipliers_and_ticks():
    assert CONTRACT_SPECS["TXF"].multiplier == 200
    assert CONTRACT_SPECS["MXF"].multiplier == 50
    assert CONTRACT_SPECS["TMF"].multiplier == 10
    assert CONTRACT_SPECS["EXF"].multiplier == 4000
    assert CONTRACT_SPECS["FXF"].multiplier == 1000
    assert CONTRACT_SPECS["TXF"].tick_size == 1
    assert CONTRACT_SPECS["EXF"].tick_size == 0.05
    assert CONTRACT_SPECS["FXF"].tick_size == 0.2
