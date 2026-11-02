"""TAIFEX option daily history: parse and import into ``qd_tw_option_daily`` (issue #67).

Two TAIFEX downloads are read:

* Daily option report ("每日選擇權交易行情下載", ``optDataDown``): one row per
  trading day, contract month (or week), strike, call/put and session with
  open / high / low / close, volume, settlement price and open interest.
* Final settlement prices ("最後結算價"): one price per product and contract
  month. TXO is a cash-settled European option, so a backtest cannot settle an
  expiring position without it.

Columns are located by header name (prefix match, with aliases), never by a
fixed position, because TAIFEX has changed the column order over the years.
Files are Big5 (cp950) or UTF-8; :func:`decode_taifex_bytes` handles both and
zip archives. Rows that cannot be parsed are counted as invalid, not guessed.

Option code helpers (``option_code``) come from :mod:`app.data_sources.tw_options`
(#59); there is a single implementation of the TAIFEX option code rules.

``trade_date`` is the ``交易日期`` exactly as published. An after-hours (``盤後``)
row of date D is the night session that opened at 15:00 on D. Every write is an
upsert keyed by (trade_date, product, expiry, strike, right, session), so a
re-run is idempotent. Network access goes through an injectable fetcher with a
fixed delay between requests; tests never reach the TAIFEX website.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.data_sources.tw_options import CALL, PUT, option_code, option_product
from app.services.taifex_history import (
    REQUEST_DELAY_SECONDS,
    Fetcher,
    TaifexFormatError,
    _number,
    _parse_date,
    _rows,
    decode_taifex_bytes,
    http_fetcher,
    month_chunks,
)
from app.services.tw_kline_store import SESSION_AFTERHOURS, SESSION_REGULAR, SOURCE_TAIFEX
from app.utils.logger import get_logger

logger = get_logger(__name__)

# The import endpoints. Verified by the owner against the TAIFEX download pages;
# the parsers do not depend on them (a manually downloaded file works too).
DAILY_OPTIONS_URL = "https://www.taifex.com.tw/cht/3/optDataDown"
FINAL_SETTLEMENT_URL = "https://www.taifex.com.tw/cht/5/fSPriceDown"

DEFAULT_OPTION_PRODUCTS = ("TXO",)

_SESSIONS = {"一般": SESSION_REGULAR, "盤後": SESSION_AFTERHOURS}
_RIGHTS = {"買權": CALL, "CALL": CALL, "C": CALL, "賣權": PUT, "PUT": PUT, "P": PUT}
# 202611 (monthly), 202611W1 (weekly), 202611F1 (Friday weekly) ...
_EXPIRY_RE = re.compile(r"^(\d{4})(\d{2})([A-Z]\d)?$")
_PRODUCT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")

# Logical column -> accepted header prefixes (first match wins).
_DAILY_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "trade_date": ("交易日期",),
    "product": ("契約",),
    "expiry": ("到期月份",),
    "strike": ("履約價",),
    "right": ("買賣權",),
    "open": ("開盤價",),
    "high": ("最高價",),
    "low": ("最低價",),
    "close": ("收盤價",),
    "volume": ("成交量",),
    "settlement": ("結算價",),
    "open_interest": ("未沖銷契約",),
}
_OPTIONAL_DAILY_COLUMNS: Dict[str, Tuple[str, ...]] = {"session": ("交易時段",)}

# Product names as they may appear in a wide settlement table header.
_PRODUCT_ALIASES: Dict[str, Tuple[str, ...]] = {"TXO": ("TXO", "臺指選擇權", "台指選擇權")}


# --------------------------------------------------------------------------- rows
@dataclass(frozen=True)
class OptionDailyRow:
    trade_date: date
    product: str
    expiry: str  # 202611 or 202611W1
    strike: float
    right: str  # call / put
    session: str  # regular / afterhours
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: float
    settlement_price: Optional[float]
    open_interest: Optional[float]
    symbol: str = ""  # TXO23000H6 for monthly series of a known product, else ""
    source: str = SOURCE_TAIFEX

    @property
    def is_monthly(self) -> bool:
        return len(self.expiry) == 6


@dataclass(frozen=True)
class OptionSettlement:
    product: str
    contract_month: str  # 202611 or 202611W1
    settlement_date: date
    price: float
    source: str = SOURCE_TAIFEX


@dataclass
class ParseResult:
    rows: List[Any] = field(default_factory=list)
    invalid: int = 0


# --------------------------------------------------------------------------- helpers
def parse_option_products(raw: Any) -> List[str]:
    """``"TXO, txo"`` -> ``["TXO"]``; raises on a malformed product code."""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    out: List[str] = []
    for item in items:
        text = str(item or "").strip().upper()
        if not text:
            continue
        if not _PRODUCT_RE.match(text):
            raise ValueError(f"invalid TAIFEX option product: {item!r}")
        if text not in out:
            out.append(text)
    return out


def parse_right(value: Any) -> Optional[str]:
    return _RIGHTS.get(str(value or "").strip().upper())


def normalize_expiry(value: Any) -> Optional[str]:
    """``" 202611 "`` -> ``202611``; None for spreads or malformed months."""
    text = re.sub(r"\s+", "", str(value or "")).upper()
    match = _EXPIRY_RE.match(text)
    if not match or not 1 <= int(match.group(2)) <= 12:
        return None
    return text


def monthly_symbol(product: str, expiry: str, strike: float, right: str) -> str:
    """``TXO23000H6`` for a monthly series of a product #59 knows; ``""`` otherwise."""
    if len(expiry) != 6 or option_product(product) is None or float(strike) != int(strike):
        return ""
    try:
        return option_code(product, strike, right, int(expiry[:4]), int(expiry[4:6]))
    except ValueError:
        return ""


def _find_header(rows: List[List[str]], columns: Dict[str, Tuple[str, ...]]) -> Tuple[int, Dict[str, int]]:
    for index, row in enumerate(rows[:15]):
        found = _match_columns(row, columns)
        if len(found) == len(columns):
            return index, found
    raise TaifexFormatError("TAIFEX option header not found: " + ", ".join(p[0] for p in columns.values()))


def _match_columns(row: List[str], columns: Dict[str, Tuple[str, ...]]) -> Dict[str, int]:
    found: Dict[str, int] = {}
    for key, prefixes in columns.items():
        for position, cell in enumerate(row):
            if any(cell.startswith(prefix) for prefix in prefixes):
                found[key] = position
                break
    return found


def _is_no_data(text: str) -> bool:
    head = text[:2000]
    return ("查無資料" in head or "無資料" in head) and "交易日期" not in head and "結算" not in head


# --------------------------------------------------------------------------- daily report
def parse_option_daily_report(text: str, *, products: Optional[Iterable[str]] = None) -> ParseResult:
    """Parse an ``optDataDown`` report. Rows without a trade keep their settlement / OI."""
    result = ParseResult()
    if _is_no_data(text):
        return result
    rows = _rows(text)
    if not rows:
        return result
    header, cols = _find_header(rows, _DAILY_COLUMNS)
    cols.update(_match_columns(rows[header], _OPTIONAL_DAILY_COLUMNS))
    wanted = set(parse_option_products(list(products))) if products is not None else set()
    for row in rows[header + 1:]:
        product = row[cols["product"]].upper() if len(row) > cols["product"] else ""
        if wanted and product not in wanted:
            continue
        parsed = _parse_daily_row(row, cols, product)
        if parsed is None:
            result.invalid += 1
            continue
        result.rows.append(parsed)
    return result


def _parse_daily_row(row: List[str], cols: Dict[str, int], product: str) -> Optional[OptionDailyRow]:
    if len(row) <= max(position for key, position in cols.items() if key != "session"):
        return None

    def cell(key: str) -> str:
        position = cols.get(key)
        return row[position] if position is not None and position < len(row) else ""

    expiry = normalize_expiry(cell("expiry"))
    right = parse_right(cell("right"))
    strike = _number(cell("strike"))
    session = _SESSIONS.get(cell("session") or "一般")
    if not _PRODUCT_RE.match(product) or expiry is None or right is None or session is None:
        return None
    if strike is None or strike <= 0:
        return None
    try:
        trade_date = _parse_date(cell("trade_date"))
    except ValueError:
        return None
    return OptionDailyRow(
        trade_date=trade_date,
        product=product,
        expiry=expiry,
        strike=float(strike),
        right=right,
        session=session,
        open=_number(cell("open")),
        high=_number(cell("high")),
        low=_number(cell("low")),
        close=_number(cell("close")),
        volume=_number(cell("volume")) or 0.0,
        settlement_price=_number(cell("settlement")),
        open_interest=_number(cell("open_interest")),
        symbol=monthly_symbol(product, expiry, strike, right),
    )


# --------------------------------------------------------------------------- final settlement
_SETTLE_DATE = ("最後結算日", "結算日")
_SETTLE_MONTH = ("契約月份", "到期月份")
_SETTLE_PRICE = ("最後結算價",)
_SETTLE_PRODUCT = ("契約", "商品")


def parse_final_settlements(text: str, *, products: Iterable[str] = DEFAULT_OPTION_PRODUCTS) -> ParseResult:
    """Parse a final settlement price table.

    Two layouts are accepted: a long table (``最後結算日, 契約, 契約月份,
    最後結算價``) and a wide one with one column per product whose header names
    the product (``臺指選擇權(TXO)``). Missing prices (``-``) are invalid rows,
    never zero.
    """
    result = ParseResult()
    wanted = parse_option_products(list(products))
    if _is_no_data(text):
        return result
    rows = _rows(text)
    for index, row in enumerate(rows[:15]):
        base = _match_columns(row, {"date": _SETTLE_DATE, "month": _SETTLE_MONTH})
        if len(base) < 2:
            continue
        price_col = _match_columns(row, {"price": _SETTLE_PRICE}).get("price")
        if price_col is not None:
            product_col = _product_column(row, exclude=set(base.values()) | {price_col})
            if product_col is not None:
                _parse_long_settlements(rows[index + 1:], base, product_col, price_col, wanted, result)
                return result
        product_cols = _wide_product_columns(row, wanted)
        if product_cols:
            _parse_wide_settlements(rows[index + 1:], base, product_cols, result)
            return result
    raise TaifexFormatError("TAIFEX final settlement header not found")


def _product_column(row: List[str], exclude: set) -> Optional[int]:
    for position, cell in enumerate(row):
        if position not in exclude and any(cell.startswith(prefix) for prefix in _SETTLE_PRODUCT):
            return position
    return None


def _wide_product_columns(row: List[str], wanted: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for product in wanted:
        aliases = _PRODUCT_ALIASES.get(product, (product,))
        for position, cell in enumerate(row):
            if any(alias in cell.upper() for alias in aliases):
                out[product] = position
                break
    return out


def _settlement(product: str, month_text: str, date_text: str, price_text: str) -> Optional[OptionSettlement]:
    month = normalize_expiry(month_text)
    price = _number(price_text)
    if month is None or price is None or price <= 0:
        return None
    try:
        day = _parse_date(date_text)
    except ValueError:
        return None
    return OptionSettlement(product, month, day, float(price))


def _parse_long_settlements(rows, base, product_col, price_col, wanted, result: ParseResult) -> None:
    width = max(base["date"], base["month"], product_col, price_col)
    for row in rows:
        product = row[product_col].upper() if len(row) > product_col else ""
        if product not in wanted:
            continue
        item = None
        if len(row) > width:
            item = _settlement(product, row[base["month"]], row[base["date"]], row[price_col])
        if item is None:
            result.invalid += 1
            continue
        result.rows.append(item)


def _parse_wide_settlements(rows, base, product_cols: Dict[str, int], result: ParseResult) -> None:
    for row in rows:
        for product, col in product_cols.items():
            if len(row) <= max(base["date"], base["month"], col):
                result.invalid += 1
                continue
            if not row[col].strip() or row[col].strip() in {"-", "--"}:
                continue  # the product has no settlement in that month (e.g. weekly rows)
            item = _settlement(product, row[base["month"]], row[base["date"]], row[col])
            if item is None:
                result.invalid += 1
                continue
            result.rows.append(item)


# --------------------------------------------------------------------------- download forms
def daily_options_form(product: str, start: date, end: date) -> Dict[str, str]:
    return {
        "down_type": "1",
        "commodity_id": product,
        "commodity_id2": "",
        "queryStartDate": start.strftime("%Y/%m/%d"),
        "queryEndDate": end.strftime("%Y/%m/%d"),
    }


def final_settlement_form(start: date, end: date) -> Dict[str, str]:
    return {
        "start_year": f"{start.year:04d}",
        "start_month": f"{start.month:02d}",
        "end_year": f"{end.year:04d}",
        "end_month": f"{end.month:02d}",
        "dlFileType": "3",
    }


# --------------------------------------------------------------------------- import
@dataclass
class OptionImportReport:
    daily_rows: int = 0
    settlements: int = 0
    invalid_rows: int = 0
    failed: List[Dict[str, str]] = field(default_factory=list)
    empty: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "daily_rows": self.daily_rows,
            "settlements": self.settlements,
            "invalid_rows": self.invalid_rows,
            "failed": list(self.failed),
            "empty": list(self.empty),
        }


class TaifexOptionsImporter:
    """Download (optional) and upsert TAIFEX option history and final settlements."""

    def __init__(
        self,
        *,
        repository: Any = None,
        fetcher: Optional[Fetcher] = None,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = REQUEST_DELAY_SECONDS,
    ) -> None:
        if repository is None:
            from app.services.tw_option_store import TWOptionRepository

            repository = TWOptionRepository()
        self.repository = repository
        self._fetcher = fetcher
        self._sleep = sleep
        self._delay = max(0.0, float(delay))
        self._requested = False

    def _fetch(self, method: str, url: str, form: Optional[Dict[str, str]] = None) -> bytes:
        if self._fetcher is None:
            self._fetcher = http_fetcher()
        if self._requested and self._delay:
            self._sleep(self._delay)
        self._requested = True
        return self._fetcher(method, url, form)

    # -------------------------------------------------------------- parsed text / bytes
    def import_daily_bytes(self, payload: bytes, *, products: Optional[Iterable[str]] = None,
                           report: Optional[OptionImportReport] = None) -> OptionImportReport:
        report = report or OptionImportReport()
        parsed = parse_option_daily_report(decode_taifex_bytes(payload), products=products)
        report.invalid_rows += parsed.invalid
        if parsed.rows:
            report.daily_rows += int(self.repository.upsert_daily(parsed.rows))
        return report

    def import_settlement_bytes(self, payload: bytes, *, products: Iterable[str] = DEFAULT_OPTION_PRODUCTS,
                                report: Optional[OptionImportReport] = None) -> OptionImportReport:
        report = report or OptionImportReport()
        parsed = parse_final_settlements(decode_taifex_bytes(payload), products=products)
        report.invalid_rows += parsed.invalid
        if parsed.rows:
            report.settlements += int(self.repository.upsert_settlements(parsed.rows))
        return report

    # -------------------------------------------------------------- downloads
    def import_daily(self, start: date, end: date,
                     products: Sequence[str] = DEFAULT_OPTION_PRODUCTS) -> OptionImportReport:
        """Download month by month; a failed month is reported, not raised, and can be re-run."""
        report = OptionImportReport()
        for product in parse_option_products(list(products)):
            for chunk_start, chunk_end in month_chunks(start, end):
                label = f"{product} {chunk_start.isoformat()}..{chunk_end.isoformat()}"
                try:
                    payload = self._fetch("POST", DAILY_OPTIONS_URL, daily_options_form(product, chunk_start, chunk_end))
                    before = report.daily_rows
                    self.import_daily_bytes(payload, products=[product], report=report)
                    if report.daily_rows == before:
                        report.empty.append(label)
                except Exception as exc:  # keep going; a re-run retries the month
                    logger.warning("TAIFEX option import failed for %s: %s", label, exc)
                    report.failed.append({"chunk": label, "error": str(exc)[:300]})
        return report

    def import_settlements(self, start: date, end: date, products: Sequence[str] = DEFAULT_OPTION_PRODUCTS,
                           report: Optional[OptionImportReport] = None) -> OptionImportReport:
        report = report or OptionImportReport()
        label = f"final-settlement {start.isoformat()}..{end.isoformat()}"
        try:
            payload = self._fetch("POST", FINAL_SETTLEMENT_URL, final_settlement_form(start, end))
            before = report.settlements
            self.import_settlement_bytes(payload, products=products, report=report)
            if report.settlements == before:
                report.empty.append(label)
        except Exception as exc:
            logger.warning("TAIFEX final settlement import failed for %s: %s", label, exc)
            report.failed.append({"chunk": label, "error": str(exc)[:300]})
        return report
