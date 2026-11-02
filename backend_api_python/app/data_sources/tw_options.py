"""TAIFEX option contract codes (market data only).

Option codes are the product code, a five-digit strike, a month letter and the
last digit of the year: ``TXO23000H6`` is the August 2026 TAIEX call at 23000,
``TXO23000T6`` the August 2026 put. Calls use the month letters ``A``-``L``
(January to December) and puts ``M``-``X``.

Products:
    TXO                 monthly TAIEX options, last trading day on the third Wednesday
    TX1 / TX2 / TX4 / TX5  weekly TAIEX options expiring on the first, second,
                        fourth or fifth Wednesday of the month (the third
                        Wednesday is the monthly TXO)

Other three-character products (stock options, Friday weeklies) parse, but
their expiry is only known from the sidecar ticker list (``settlementDate``).

The strategy backtest engine does not model premiums, expiry, exercise or
margin, so option contracts are restricted to quotes and K-lines: backtests
and strategy deployments reject them (``strategyV2.optionsBacktestUnsupported``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional

from app.data_sources.tw_futures_contracts import (
    TradingDayCheck,
    resolve_year,
    xtai_is_trading_day,
)

CALL = "call"
PUT = "put"
CALL_MONTH_CODES = "ABCDEFGHIJKL"
PUT_MONTH_CODES = "MNOPQRSTUVWX"

OPTION_RE = re.compile(r"^([A-Z][A-Z0-9]{2})(\d{5})([A-X])(\d)$")

OPTIONS_UNSUPPORTED_ERROR = "strategyV2.optionsBacktestUnsupported"


@dataclass(frozen=True)
class OptionProduct:
    product: str
    name: str
    # Wednesday of the month the series expires on (3 = the monthly TXO).
    wednesday: int
    multiplier: float
    underlying: str


# Owner-verified against the TAIFEX contract specifications before relying on it.
OPTION_PRODUCTS: Dict[str, OptionProduct] = {
    "TXO": OptionProduct("TXO", "TAIEX Options", 3, 50.0, "IX0001"),
    "TX1": OptionProduct("TX1", "TAIEX Weekly Options (Week 1)", 1, 50.0, "IX0001"),
    "TX2": OptionProduct("TX2", "TAIEX Weekly Options (Week 2)", 2, 50.0, "IX0001"),
    "TX4": OptionProduct("TX4", "TAIEX Weekly Options (Week 4)", 4, 50.0, "IX0001"),
    "TX5": OptionProduct("TX5", "TAIEX Weekly Options (Week 5)", 5, 50.0, "IX0001"),
}


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    product: str
    strike: int
    right: str  # "call" / "put"
    year: int
    month: int
    # Last trading day from the TAIFEX calendar; None for products whose
    # schedule is not known here (use the sidecar ticker ``settlementDate``).
    expiry: Optional[date]

    @property
    def expiry_month(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def option_product(product: str) -> Optional[OptionProduct]:
    return OPTION_PRODUCTS.get(str(product or "").strip().upper())


def is_option_code(symbol: Any) -> bool:
    """Whether ``symbol`` (``TXO23000H6`` or ``TWFutures:TXO23000H6``) is shaped like an option."""
    text = str(symbol or "").strip().upper()
    if text.startswith("TWFUTURES:"):
        text = text.split(":", 1)[1]
    text = text.split("@", 1)[0].strip()
    return bool(OPTION_RE.match(text))


def option_month_code(month: int, right: str) -> str:
    if not 1 <= int(month) <= 12:
        raise ValueError(f"month must be 1-12, got {month!r}")
    letters = CALL_MONTH_CODES if _normalize_right(right) == CALL else PUT_MONTH_CODES
    return letters[int(month) - 1]


def option_code(product: str, strike: float, right: str, year: int, month: int) -> str:
    """``("TXO", 23000, "call", 2026, 8)`` -> ``TXO23000H6``."""
    value = int(round(float(strike)))
    if not 0 <= value <= 99999:
        raise ValueError(f"strike must fit five digits, got {strike!r}")
    return f"{str(product).strip().upper()}{value:05d}{option_month_code(month, right)}{int(year) % 10}"


def nth_wednesday(year: int, month: int, n: int) -> Optional[date]:
    """The ``n``-th Wednesday of the month, or None when the month has fewer."""
    first = date(year, month, 1)
    day = first + timedelta(days=(2 - first.weekday()) % 7 + 7 * (int(n) - 1))
    return day if day.month == month else None


def option_expiry_date(
    product: str,
    year: int,
    month: int,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> Optional[date]:
    """Last trading day of a TXO / TXn series, moved forward past holidays.

    None for products without a known schedule, and for a fifth-week series in
    a month that has only four Wednesdays.
    """
    spec = option_product(product)
    if spec is None:
        return None
    day = nth_wednesday(year, month, spec.wednesday)
    if day is None:
        return None
    check = is_trading_day or xtai_is_trading_day
    for _ in range(10):
        if check(day):
            return day
        day += timedelta(days=1)
    return nth_wednesday(year, month, spec.wednesday)


def parse_option_symbol(
    symbol: Any,
    reference: date,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> Optional[OptionContract]:
    """Decode ``TXO23000H6`` / ``TWFutures:TX123000T6``; None when it is not a valid option code.

    The year digit resolves to the decade that puts the series month closest to
    ``reference`` (options are listed at most about a year ahead).
    """
    text = str(symbol or "").strip().upper()
    if text.startswith("TWFUTURES:"):
        text = text.split(":", 1)[1].strip()
    match = OPTION_RE.match(text)
    if not match:
        return None
    product, strike, letter, digit = match.group(1), int(match.group(2)), match.group(3), int(match.group(4))
    if letter in CALL_MONTH_CODES:
        right, month = CALL, CALL_MONTH_CODES.index(letter) + 1
    else:
        right, month = PUT, PUT_MONTH_CODES.index(letter) + 1
    year = resolve_year(digit, month, reference)
    expiry: Optional[date] = None
    if option_product(product) is not None:
        expiry = option_expiry_date(product, year, month, is_trading_day)
        if expiry is None:  # e.g. TX5 in a month without a fifth Wednesday
            return None
    return OptionContract(text, product, strike, right, year, month, expiry)


def first_option_instrument(instruments: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """The first ``TWFutures`` option among ``{market, symbol}`` items, or None."""
    for item in instruments:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("market") or "").strip().lower() != "twfutures":
            continue
        symbol = str(item.get("symbol") or "")
        if is_option_code(symbol):
            return f"TWFutures:{symbol.strip().upper()}"
    return None


def _normalize_right(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"call", "c"}:
        return CALL
    if raw in {"put", "p"}:
        return PUT
    raise ValueError(f"right must be call or put, got {value!r}")
