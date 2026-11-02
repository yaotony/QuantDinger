"""TAIFEX public futures history: parse and import into ``qd_tw_klines`` (``source=taifex``).

Fubon serves no futures history, so the daily accumulation (``tw_kline_collector``)
starts on the day it was switched on. Two TAIFEX downloads fill the past:

* Daily market report ("每日行情下載", ``futDataDown``): one row per contract month
  and session with open / high / low / close / volume, back many years. Stored as
  ``timeframe=1D`` per contract (``TXFH6``) and session. A ``一般`` (regular) row
  of date D is trading day D; a ``盤後`` (after-hours) row of date D is the night
  session that opened at 15:00 on D, which belongs to the next trading day. Both
  are labelled at Asia/Taipei midnight of their trading day, the label the
  ``TWFutures`` data source uses for daily bars.
* Tick file of the last 30 days (``Daily_YYYY_MM_DD``): every trade with
  成交日期, 商品代號, 到期月份(週別), 成交時間, 成交價格, 成交數量(B+S), ... .
  Aggregated into ``timeframe=1m`` bars per contract; the session follows the
  bar's wall-clock time (day 08:30-14:00, night 14:50-05:10, as for Fubon bars).
  成交數量(B+S) counts both sides, so contracts = quantity / 2.

Weekly contracts (``202605W4``) and spreads (a ``/`` in the month or code) are
skipped. Files are Big5 (cp950) or UTF-8; both are decoded. Writes are upserts
that never replace a ``source=fubon`` bar (``TWKlineRepository.upsert_bars``), so
re-running an import is idempotent.

TAIFEX product codes differ from the Fubon codes used as QuantDinger symbols
(``TX`` -> ``TXF``); :data:`TAIFEX_TO_FUBON` is the single mapping. Network access
goes through an injectable fetcher with a fixed delay between requests.
"""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.data_sources.tw_futures import bar_session
from app.data_sources.tw_futures_contracts import (
    TAIPEI_TZ,
    TradingDayCheck,
    contract_code,
    next_trading_day,
    xtai_is_trading_day,
)
from app.services.tw_kline_store import (
    MARKET_FUTURES,
    SESSION_AFTERHOURS,
    SESSION_REGULAR,
    SOURCE_TAIFEX,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# TAIFEX product code -> Fubon / QuantDinger product code.
TAIFEX_TO_FUBON: Dict[str, str] = {
    "TX": "TXF",   # TAIEX Futures
    "MTX": "MXF",  # Mini-TAIEX Futures
    "TMF": "TMF",  # Micro TAIEX Futures
    "TE": "EXF",   # Electronic Sector Index Futures
    "TF": "FXF",   # Finance Sector Index Futures
    "ZEF": "ZEF",  # Mini Electronic Sector Index Futures
    "ZFF": "ZFF",  # Mini Finance Sector Index Futures
}
FUBON_TO_TAIFEX: Dict[str, str] = {fubon: taifex for taifex, fubon in TAIFEX_TO_FUBON.items()}

DEFAULT_PRODUCTS = ("TX", "MTX")

DAILY_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
TICKS_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{day}.zip"
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60

TIMEFRAME_DAILY = "1D"
TIMEFRAME_MINUTE = "1m"

_PURE_MONTH_RE = re.compile(r"^(\d{4})(\d{2})$")
_NO_DATA_MARKERS = ("查無資料", "無資料")

_DAILY_SESSION = {"一般": SESSION_REGULAR, "盤後": SESSION_AFTERHOURS}


class TaifexFormatError(ValueError):
    """A TAIFEX file that is neither the expected CSV nor a "no data" page."""


# --------------------------------------------------------------------------- products
def to_fubon_product(taifex_code: str) -> Optional[str]:
    """``TX`` -> ``TXF``; None for products outside the mapping."""
    return TAIFEX_TO_FUBON.get(str(taifex_code or "").strip().upper())


def to_taifex_product(code: str) -> Optional[str]:
    """``TXF`` or ``TX`` -> ``TX``; None when unknown."""
    value = str(code or "").strip().upper()
    if value in TAIFEX_TO_FUBON:
        return value
    return FUBON_TO_TAIFEX.get(value)


def parse_products(raw: Any) -> List[str]:
    """``"TX,MXF"`` -> ``["TX", "MTX"]`` (TAIFEX codes); raises on an unknown product."""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    out: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        product = to_taifex_product(text)
        if product is None:
            raise ValueError(f"unknown TAIFEX futures product: {text}")
        if product not in out:
            out.append(product)
    return out


def contract_symbol(taifex_code: str, month_field: str) -> Optional[str]:
    """``("TX", "202608")`` -> ``TXFH6``; None for weekly, spread or unknown rows."""
    product = to_fubon_product(taifex_code)
    month_text = str(month_field or "").strip()
    if product is None or "/" in str(taifex_code) or "/" in month_text:
        return None
    match = _PURE_MONTH_RE.match(month_text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return contract_code(product, year, month)


# --------------------------------------------------------------------------- decoding
def decode_taifex_bytes(payload: bytes) -> str:
    """Decode a TAIFEX file: UTF-8 (with or without BOM), else Big5 / cp950.

    A zip archive is unpacked first (its first ``.csv`` member, else the first file).
    """
    data = bytes(payload or b"")
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if not names:
                raise TaifexFormatError("empty zip archive")
            csv_names = [name for name in names if name.lower().endswith(".csv")]
            data = archive.read((csv_names or names)[0])
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    for encoding in ("cp950", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp950", errors="replace")


def _rows(text: str) -> List[List[str]]:
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]


def _header_index(rows: List[List[str]], required: Sequence[str]) -> Tuple[int, Dict[str, int]]:
    """Locate the header row containing every ``required`` column (prefix match)."""
    for index, row in enumerate(rows[:10]):
        columns: Dict[str, int] = {}
        for name in required:
            for position, cell in enumerate(row):
                if cell.startswith(name):
                    columns[name] = position
                    break
        if len(columns) == len(required):
            return index, columns
    raise TaifexFormatError("TAIFEX header not found: " + ", ".join(required))


def _is_no_data(text: str) -> bool:
    head = text[:2000]
    return any(marker in head for marker in _NO_DATA_MARKERS)


def _number(value: str) -> Optional[float]:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(value: str) -> date:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8:
        raise ValueError(f"bad TAIFEX date: {value!r}")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def _midnight_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ).timestamp())


# --------------------------------------------------------------------------- daily report
@dataclass(frozen=True)
class DailyBar:
    symbol: str  # contract, e.g. TXFH6
    session: str  # regular / afterhours
    trade_date: date  # trading day the bar belongs to
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_bar(self) -> Dict[str, Any]:
        return {
            "time": _midnight_ts(self.trade_date),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


_DAILY_COLUMNS = ("交易日期", "契約", "到期月份", "開盤價", "最高價", "最低價", "收盤價", "成交量")
# Reports from before the night session existed may lack 交易時段; those rows are regular.
_DAILY_SESSION_COLUMN = "交易時段"


def parse_daily_report(
    text: str,
    *,
    products: Optional[Iterable[str]] = None,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> List[DailyBar]:
    """Parse a ``futDataDown`` daily report into per-contract session bars.

    ``products`` (TAIFEX codes) limits the rows; rows without a trade (``-`` prices)
    are skipped. Returns [] for a "no data" page.
    """
    if _is_no_data(text) and "交易日期" not in text[:2000]:
        return []
    rows = _rows(text)
    if not rows:
        return []
    header, cols = _header_index(rows, _DAILY_COLUMNS)
    session_col = next(
        (i for i, cell in enumerate(rows[header]) if cell.startswith(_DAILY_SESSION_COLUMN)), None
    )
    wanted = set(parse_products(list(products))) if products is not None else set()
    check = is_trading_day or xtai_is_trading_day
    out: List[DailyBar] = []
    for row in rows[header + 1:]:
        if len(row) <= max(cols.values()):
            continue
        code = row[cols["契約"]].upper()
        if wanted and code not in wanted:
            continue
        symbol = contract_symbol(code, row[cols["到期月份"]])
        session_text = row[session_col] if session_col is not None and session_col < len(row) else ""
        session = _DAILY_SESSION.get(session_text or "一般")
        if symbol is None or session is None:
            continue
        prices = [_number(row[cols[name]]) for name in ("開盤價", "最高價", "最低價", "收盤價")]
        if any(price is None for price in prices):
            continue
        try:
            day = _parse_date(row[cols["交易日期"]])
        except ValueError:
            continue
        # The after-hours row of date D opened at 15:00 on D: next trading day's night.
        trade_day = next_trading_day(day, check) if session == SESSION_AFTERHOURS else day
        open_, high, low, close = (float(p) for p in prices)  # type: ignore[arg-type]
        out.append(DailyBar(symbol, session, trade_day, open_, high, low, close, _number(row[cols["成交量"]]) or 0.0))
    return out


# --------------------------------------------------------------------------- tick file
@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: int  # unix seconds
    price: float
    contracts: float  # B+S / 2


_TICK_COLUMNS = ("成交日期", "商品代號", "到期月份", "成交時間", "成交價格", "成交數量")


def parse_tick_file(text: str, *, products: Optional[Iterable[str]] = None) -> List[Tick]:
    """Parse a ``Daily_YYYY_MM_DD`` tick file, in file order.

    成交日期 + 成交時間 are Asia/Taipei wall time. Quantity B+S is halved.
    """
    rows = _rows(text)
    if not rows:
        return []
    header, cols = _header_index(rows, _TICK_COLUMNS)
    wanted = set(parse_products(list(products))) if products is not None else set()
    out: List[Tick] = []
    for row in rows[header + 1:]:
        if len(row) <= max(cols.values()):
            continue
        code = row[cols["商品代號"]].upper()
        if wanted and code not in wanted:
            continue
        symbol = contract_symbol(code, row[cols["到期月份"]])
        if symbol is None:
            continue
        price = _number(row[cols["成交價格"]])
        quantity = _number(row[cols["成交數量"]])
        clock = re.sub(r"\D", "", row[cols["成交時間"]].split(".", 1)[0]).zfill(6)
        if price is None or quantity is None or len(clock) != 6:
            continue
        try:
            day = _parse_date(row[cols["成交日期"]])
            moment = datetime.combine(
                day, dtime(int(clock[:2]), int(clock[2:4]), int(clock[4:6])), tzinfo=TAIPEI_TZ
            )
        except ValueError:
            continue
        out.append(Tick(symbol, int(moment.timestamp()), price, quantity / 2.0))
    return out


def ticks_to_minute_bars(ticks: Iterable[Tick]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """1m bars per (contract, session); ticks outside both sessions are dropped.

    Open / close follow file order within a minute (TAIFEX lists trades in order).
    """
    buckets: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = {}
    ordered = sorted(enumerate(ticks), key=lambda item: (item[1].ts, item[0]))
    for _, tick in ordered:
        minute = tick.ts - tick.ts % 60
        session = bar_session(minute)
        if session is None:
            continue
        bars = buckets.setdefault((tick.symbol, session), {})
        bar = bars.get(minute)
        if bar is None:
            bars[minute] = {
                "time": minute,
                "open": tick.price,
                "high": tick.price,
                "low": tick.price,
                "close": tick.price,
                "volume": tick.contracts,
            }
            continue
        bar["high"] = max(bar["high"], tick.price)
        bar["low"] = min(bar["low"], tick.price)
        bar["close"] = tick.price
        bar["volume"] = bar["volume"] + tick.contracts
    return {key: [bars[m] for m in sorted(bars)] for key, bars in buckets.items()}


# --------------------------------------------------------------------------- download
Fetcher = Callable[[str, str, Optional[Dict[str, str]]], bytes]


def http_fetcher(timeout: int = REQUEST_TIMEOUT_SECONDS) -> Fetcher:
    """``fetch(method, url, form)`` over ``requests``; raises on HTTP errors."""
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": "QuantDinger/TAIFEX-History"})

    def fetch(method: str, url: str, form: Optional[Dict[str, str]] = None) -> bytes:
        if method.upper() == "POST":
            response = session.post(url, data=form or {}, timeout=timeout)
        else:
            response = session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.content

    return fetch


def month_chunks(start: date, end: date) -> List[Tuple[date, date]]:
    """Split ``[start, end]`` into calendar-month pieces (TAIFEX caps a query at one month)."""
    out: List[Tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        nxt = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        out.append((cursor, min(end, nxt - timedelta(days=1))))
        cursor = nxt
    return out


def daily_report_form(product: str, start: date, end: date) -> Dict[str, str]:
    return {
        "down_type": "1",
        "commodity_id": product,
        "commodity_id2": "",
        "queryStartDate": start.strftime("%Y/%m/%d"),
        "queryEndDate": end.strftime("%Y/%m/%d"),
    }


def tick_file_url(day: date) -> str:
    return TICKS_URL.format(day=day.strftime("%Y_%m_%d"))


# --------------------------------------------------------------------------- import
@dataclass
class ImportReport:
    written: Dict[str, int] = field(default_factory=dict)  # "TXFH6/regular/1D" -> bars
    failed: List[Dict[str, str]] = field(default_factory=list)
    empty: List[str] = field(default_factory=list)

    def add(self, symbol: str, session: str, timeframe: str, count: int) -> None:
        key = f"{symbol}/{session}/{timeframe}"
        self.written[key] = self.written.get(key, 0) + int(count)

    @property
    def total(self) -> int:
        return sum(self.written.values())

    def as_dict(self) -> Dict[str, Any]:
        return {"written": dict(self.written), "total": self.total, "failed": list(self.failed), "empty": list(self.empty)}


class TaifexHistoryImporter:
    """Download (optional) and import TAIFEX history into ``qd_tw_klines``."""

    def __init__(
        self,
        *,
        repository: Any = None,
        fetcher: Optional[Fetcher] = None,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = REQUEST_DELAY_SECONDS,
        is_trading_day: Optional[TradingDayCheck] = None,
    ) -> None:
        if repository is None:
            from app.services.tw_kline_store import TWKlineRepository

            repository = TWKlineRepository()
        self.repository = repository
        self._fetcher = fetcher
        self._sleep = sleep
        self._delay = max(0.0, float(delay))
        self._is_trading_day = is_trading_day or xtai_is_trading_day
        self._requested = False

    # -------------------------------------------------------------- network
    def _fetch(self, method: str, url: str, form: Optional[Dict[str, str]] = None) -> bytes:
        if self._fetcher is None:
            self._fetcher = http_fetcher()
        if self._requested and self._delay:
            self._sleep(self._delay)
        self._requested = True
        return self._fetcher(method, url, form)

    # -------------------------------------------------------------- writes
    def _write_daily(self, bars: List[DailyBar], report: ImportReport) -> None:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for bar in bars:
            grouped.setdefault((bar.symbol, bar.session), []).append(bar.as_bar())
        for (symbol, session), rows in sorted(grouped.items()):
            count = self.repository.upsert_bars(
                market=MARKET_FUTURES, symbol=symbol, session=session,
                timeframe=TIMEFRAME_DAILY, bars=rows, source=SOURCE_TAIFEX,
            )
            report.add(symbol, session, TIMEFRAME_DAILY, count)

    def _write_minutes(self, minutes: Dict[Tuple[str, str], List[Dict[str, Any]]], report: ImportReport) -> None:
        for (symbol, session), rows in sorted(minutes.items()):
            count = self.repository.upsert_bars(
                market=MARKET_FUTURES, symbol=symbol, session=session,
                timeframe=TIMEFRAME_MINUTE, bars=rows, source=SOURCE_TAIFEX,
            )
            report.add(symbol, session, TIMEFRAME_MINUTE, count)

    # -------------------------------------------------------------- daily report
    def import_daily_text(self, text: str, *, products: Optional[Iterable[str]] = None,
                          report: Optional[ImportReport] = None) -> ImportReport:
        report = report or ImportReport()
        bars = parse_daily_report(text, products=products, is_trading_day=self._is_trading_day)
        self._write_daily(bars, report)
        return report

    def import_daily(self, start: date, end: date, products: Sequence[str] = DEFAULT_PRODUCTS) -> ImportReport:
        """Download and store daily bars month by month; failed months are reported, not raised."""
        report = ImportReport()
        for product in parse_products(list(products)):
            for chunk_start, chunk_end in month_chunks(start, end):
                label = f"{product} {chunk_start.isoformat()}..{chunk_end.isoformat()}"
                try:
                    payload = self._fetch("POST", DAILY_URL, daily_report_form(product, chunk_start, chunk_end))
                    text = decode_taifex_bytes(payload)
                    before = report.total
                    self.import_daily_text(text, products=[product], report=report)
                    if report.total == before:
                        report.empty.append(label)
                except Exception as exc:  # keep going; a re-run retries the month
                    logger.warning("TAIFEX daily import failed for %s: %s", label, exc)
                    report.failed.append({"chunk": label, "error": str(exc)[:300]})
        return report

    # -------------------------------------------------------------- ticks
    def import_tick_bytes(self, payload: bytes, *, products: Optional[Iterable[str]] = None,
                          report: Optional[ImportReport] = None) -> ImportReport:
        report = report or ImportReport()
        ticks = parse_tick_file(decode_taifex_bytes(payload), products=products)
        self._write_minutes(ticks_to_minute_bars(ticks), report)
        return report

    def download_tick_file(self, day: date) -> bytes:
        return self._fetch("GET", tick_file_url(day))

    def import_tick_day(self, day: date, products: Sequence[str] = DEFAULT_PRODUCTS) -> ImportReport:
        """Download ``Daily_YYYY_MM_DD`` for ``day`` and store its 1m bars (raises on failure)."""
        return self.import_tick_bytes(self.download_tick_file(day), products=products)
