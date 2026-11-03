"""TAIFEX futures contract codes, expiry and front-month selection.

Shared by the ``TWFutures`` data source (continuous-code stitching) and the daily
1m K-line collector (``app.services.tw_kline_collector``), so both pick the same
near-month contract for the same instant.

Contract codes are the product code, a month letter ``A``-``L`` (January to
December) and the last digit of the year: ``TXFH6`` is the August 2026 TAIEX
future. A contract expires on its last trading day (the third Wednesday of the
contract month, moved to the next trading day when that is a holiday); its day
session closes at 13:30 that day, and from then on the next month is the near
month. The night session that evening already trades the next contract.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MONTH_CODES = "ABCDEFGHIJKL"
# The expiring contract stops trading at 13:30 on its last trading day.
EXPIRY_CUTOFF = dtime(13, 30)

PRODUCT_RE = re.compile(r"^[A-Z]{3}$")
CONTRACT_RE = re.compile(r"^([A-Z]{3})([A-L])(\d)$")

TradingDayCheck = Callable[[date], bool]


@dataclass(frozen=True)
class ContractSpec:
    """TAIFEX contract terms: TWD per index point and minimum price fluctuation."""

    product: str
    multiplier: float
    tick_size: float
    name: str


# Index futures served by the Fubon sidecar. Owner-verified against the TAIFEX
# contract specifications before relying on backtest P&L.
CONTRACT_SPECS: Dict[str, ContractSpec] = {
    "TXF": ContractSpec("TXF", 200.0, 1.0, "TAIEX Futures"),
    "MXF": ContractSpec("MXF", 50.0, 1.0, "Mini-TAIEX Futures"),
    "TMF": ContractSpec("TMF", 10.0, 1.0, "Micro TAIEX Futures"),
    "EXF": ContractSpec("EXF", 4000.0, 0.05, "Electronic Sector Index Futures"),
    "FXF": ContractSpec("FXF", 1000.0, 0.2, "Finance Sector Index Futures"),
    "ZEF": ContractSpec("ZEF", 500.0, 0.05, "Mini Electronic Sector Index Futures"),
    "ZFF": ContractSpec("ZFF", 250.0, 0.2, "Mini Finance Sector Index Futures"),
}


def contract_spec(product: str) -> Optional[ContractSpec]:
    return CONTRACT_SPECS.get(str(product or "").strip().upper())


@dataclass(frozen=True)
class ContractMonth:
    product: str
    year: int
    month: int

    @property
    def code(self) -> str:
        return contract_code(self.product, self.year, self.month)


# --------------------------------------------------------------------------- codes
def month_code(month: int) -> str:
    """``1`` -> ``A`` ... ``12`` -> ``L``."""
    if not 1 <= int(month) <= 12:
        raise ValueError(f"month must be 1-12, got {month!r}")
    return MONTH_CODES[int(month) - 1]


def contract_code(product: str, year: int, month: int) -> str:
    """``("TXF", 2026, 8)`` -> ``TXFH6``."""
    return f"{str(product).strip().upper()}{month_code(month)}{int(year) % 10}"


def parse_contract(symbol: str, reference: date) -> Optional[ContractMonth]:
    """Decode ``TXFH6`` into product / year / month.

    The code carries only the last digit of the year; the decade is the one
    that puts the contract month closest to ``reference`` (contracts are listed
    at most about a year ahead).
    """
    match = CONTRACT_RE.match(str(symbol or "").strip().upper())
    if not match:
        return None
    product, letter, digit = match.group(1), match.group(2), int(match.group(3))
    month = MONTH_CODES.index(letter) + 1
    return ContractMonth(product, resolve_year(digit, month, reference), month)


def resolve_year(digit: int, month: int, reference: date) -> int:
    """The year ending in ``digit`` that puts ``month`` closest to ``reference``."""
    ref_index = reference.year * 12 + reference.month
    best: Optional[Tuple[int, int]] = None
    for year in range(reference.year - 9, reference.year + 10):
        if year % 10 != int(digit):
            continue
        distance = abs(year * 12 + int(month) - ref_index)
        if best is None or distance < best[0]:
            best = (distance, year)
    assert best is not None
    return best[1]


def next_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


# --------------------------------------------------------------------------- calendar
# Unscheduled full-day closures of TWSE and TAIFEX (typhoon days and similar)
# that the ``exchange_calendars`` XTAI calendar does not know about. Every
# Taiwan trading-day check goes through :func:`xtai_is_trading_day`, so a day
# listed here is closed for TWStock, TWFutures, the collectors and the backfills.
# Add newly announced closures here (with the reason); until a release ships,
# operators can add them through ``TW_MARKET_CLOSED_DAYS``.
BUILTIN_TW_MARKET_CLOSED_DAYS: Dict[date, str] = {
    date(2026, 10, 23): "typhoon day: TWSE and TAIFEX closed all day",
}

TW_MARKET_CLOSED_DAYS_ENV = "TW_MARKET_CLOSED_DAYS"


@lru_cache(maxsize=32)
def _parse_closed_days(raw: str) -> FrozenSet[date]:
    out = set()
    for item in raw.replace(";", ",").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            out.add(date.fromisoformat(text))
        except ValueError:
            logger.warning("%s: ignoring invalid date %r (expected YYYY-MM-DD)", TW_MARKET_CLOSED_DAYS_ENV, text)
    return frozenset(out)


def configured_closed_days(raw: Optional[str] = None) -> FrozenSet[date]:
    """Closed days from ``TW_MARKET_CLOSED_DAYS`` (comma-separated ISO dates)."""
    text = os.getenv(TW_MARKET_CLOSED_DAYS_ENV, "") if raw is None else raw
    return _parse_closed_days(str(text or ""))


def tw_market_closed_days() -> FrozenSet[date]:
    """Built-in unscheduled closures plus the configured ones."""
    return frozenset(BUILTIN_TW_MARKET_CLOSED_DAYS) | configured_closed_days()


def is_tw_market_closed_day(day: date) -> bool:
    """Whether ``day`` is an unscheduled closure the XTAI calendar does not list."""
    return day in BUILTIN_TW_MARKET_CLOSED_DAYS or day in configured_closed_days()


@lru_cache(maxsize=4096)
def _xtai_is_session(day: date) -> bool:
    import pandas as pd

    from app.services.market_schedule import _calendar

    return bool(_calendar("TWStock").is_session(pd.Timestamp(day)))


def xtai_is_trading_day(day: date) -> bool:
    """XTAI trading day, minus the unscheduled closures (:func:`tw_market_closed_days`).

    Falls back to Monday-Friday when the calendar is unavailable.
    """
    if is_tw_market_closed_day(day):
        return False
    try:
        return _xtai_is_session(day)
    except Exception:
        return day.weekday() < 5


def next_trading_day(day: date, is_trading_day: Optional[TradingDayCheck] = None) -> date:
    """The first trading day strictly after ``day``."""
    check = is_trading_day or xtai_is_trading_day
    candidate = day + timedelta(days=1)
    for _ in range(31):
        if check(candidate):
            return candidate
        candidate += timedelta(days=1)
    return day + timedelta(days=1)


def third_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (2 - first.weekday()) % 7  # Wednesday == 2
    return first + timedelta(days=offset + 14)


def expiry_date(year: int, month: int, is_trading_day: Optional[TradingDayCheck] = None) -> date:
    """Last trading day: the third Wednesday, or the next trading day after a holiday."""
    check = is_trading_day or xtai_is_trading_day
    day = third_wednesday(year, month)
    for _ in range(10):
        if check(day):
            return day
        day += timedelta(days=1)
    return third_wednesday(year, month)


def _as_taipei(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=TAIPEI_TZ)
    return moment.astimezone(TAIPEI_TZ)


def is_expired(expiry: date, as_of: "date | datetime") -> bool:
    """Whether a contract whose last trading day is ``expiry`` no longer trades at ``as_of``.

    A ``datetime`` (naive values are read as Asia/Taipei) expires the contract at
    13:30 on its last trading day. A plain ``date`` means "the start of that
    trading day", so the contract still counts on its own last day.
    """
    if isinstance(as_of, datetime):
        local = _as_taipei(as_of)
        day = local.date()
        return expiry < day or (expiry == day and local.time() >= EXPIRY_CUTOFF)
    return expiry < as_of


def expiry_moment(year: int, month: int, is_trading_day: Optional[TradingDayCheck] = None) -> datetime:
    return datetime.combine(expiry_date(year, month, is_trading_day), EXPIRY_CUTOFF, tzinfo=TAIPEI_TZ)


# --------------------------------------------------------------------------- near month (tickers)
def _ticker_date(row: Dict[str, Any], keys: Sequence[str]) -> Optional[date]:
    for key in keys:
        value = row.get(key)
        if not value:
            continue
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def near_month_contracts(
    tickers: Sequence[Dict[str, Any]],
    product: str,
    as_of: "date | datetime",
    *,
    count: int = 2,
) -> List[str]:
    """The ``count`` nearest contracts of ``product`` still trading at ``as_of``.

    ``tickers`` are sidecar ``/v1/futopt/tickers`` rows. A contract has expired
    once :func:`is_expired` says so for its ``endDate`` (``settlementDate`` when
    absent); the rest are ordered by ``settlementDate``, so the result is the
    near month followed by the next month. Spreads, options and other products
    are ignored.
    """
    product = str(product or "").strip().upper()
    candidates: List[Tuple[date, str]] = []
    for row in tickers:
        symbol = str(row.get("symbol") or "").strip().upper()
        match = CONTRACT_RE.match(symbol)
        if not match or match.group(1) != product:
            continue
        ends = _ticker_date(row, ("endDate", "settlementDate"))
        settles = _ticker_date(row, ("settlementDate", "endDate"))
        if ends is None or settles is None or is_expired(ends, as_of):
            continue
        candidates.append((settles, symbol))
    candidates.sort()
    out: List[str] = []
    for _, symbol in candidates:
        if symbol not in out:
            out.append(symbol)
        if len(out) >= count:
            break
    return out


# --------------------------------------------------------------------------- near month (calendar)
def front_month(
    product: str,
    moment: datetime,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> ContractMonth:
    """Near-month contract trading at ``moment`` derived from the TAIFEX calendar.

    Used for historical stitching, where the sidecar ticker list (current
    contracts only) cannot answer.
    """
    local = _as_taipei(moment)
    year, month = local.year, local.month
    if is_expired(expiry_date(year, month, is_trading_day), local):
        year, month = next_month(year, month)
    return ContractMonth(str(product).strip().upper(), year, month)


def front_month_segments(
    product: str,
    start_ts: int,
    end_ts: int,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> List[Tuple[str, int, int]]:
    """Split ``[start_ts, end_ts)`` into ``(contract, start, end)`` near-month pieces.

    Each piece ends at the 13:30 expiry of its contract, where the next month
    takes over (front month, unadjusted).
    """
    out: List[Tuple[str, int, int]] = []
    cursor = int(start_ts)
    end = int(end_ts)
    while cursor < end:
        contract = front_month(product, datetime.fromtimestamp(cursor, tz=TAIPEI_TZ), is_trading_day)
        roll = int(expiry_moment(contract.year, contract.month, is_trading_day).timestamp())
        piece_end = min(end, roll)
        if piece_end <= cursor:  # defensive: never loop forever
            piece_end = end
        out.append((contract.code, cursor, piece_end))
        cursor = piece_end
    return out
