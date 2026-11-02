"""Daily accumulation of Taiwan 1m K-lines from the Fubon sidecar into ``qd_tw_klines``.

Fubon only serves stock minute bars for the latest 5 trading days and futures
minute bars for the current session, so a periodic Celery Beat tick
(``quantdinger.tasks.tw_kline_collect``) stores them after each session closes:

* stocks / indices (``TWStock``): after 14:00 Asia/Taipei on each XTAI trading day,
  session ``regular``. The fetch returns the latest 5 trading days, so a failed
  day is retried until it falls out of that window.
* futures (``TWFutures``): the day session after 13:50 (``regular``) and the night
  session after 05:10 the next morning (``afterhours``), for the near-month and
  next-month contracts (``TXFH6``, never a continuous code). A failed session is
  retried until the next trading day's 08:30.

Every (market, symbol, session, trading day) outcome is written to
``qd_tw_kline_collect_status``: ``success``, ``failed`` (retried by the next tick)
or ``missed`` (the retry window closed; a gap that is logged as a warning).
Non-trading days (``exchange_calendars`` XTAI) are never fetched.

The collection list is ``TW_KLINE_COLLECT_SYMBOLS`` plus every Taiwan symbol on a
user watchlist.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from app.data_sources.tw_stock import FubonSidecarError, normalize_tw_symbol
from app.services.tw_kline_store import (
    MARKET_FUTURES,
    MARKET_STOCK,
    SESSION_AFTERHOURS,
    SESSION_REGULAR,
    SOURCE_FUBON,
    STATUS_FAILED,
    STATUS_MISSED,
    STATUS_SUCCESS,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

DEFAULT_COLLECT_SYMBOLS = (
    "TWStock:IX0001,TWStock:2330.TW,TWStock:0050.TW,TWFutures:TXF,TWFutures:MXF"
)

# When a session's bars become available, Asia/Taipei wall time.
STOCK_READY = dtime(14, 0)
FUTURES_REGULAR_READY = dtime(13, 50)
FUTURES_AFTERHOURS_READY = dtime(5, 10)  # the calendar day after the night session opens
# Futures sessions are retried until this time on the next trading day.
FUTURES_EXPIRE = dtime(8, 30)
# Stock days are retried until 08:00 on the Nth trading day after, while the
# sidecar's latest-5-trading-days minute history still contains them.
STOCK_EXPIRE = dtime(8, 0)
STOCK_RETRY_SESSIONS = 3
# How far back the tick looks for trading days that are still pending.
LOOKBACK_DAYS = 7

# Bars accepted for a session, Asia/Taipei wall time (generous around the
# official 09:00-13:30 / 08:45-13:45 / 15:00-05:00 hours).
_STOCK_WINDOW = (dtime(8, 30), dtime(14, 0))
_FUTURES_REGULAR_WINDOW = (dtime(8, 30), dtime(14, 0))
_FUTURES_AFTERHOURS_START = dtime(14, 50)
_FUTURES_AFTERHOURS_END = dtime(5, 10)  # next calendar day

_FUTURES_PRODUCT_RE = re.compile(r"^[A-Z]{3}$")
# Month code A-L (Jan-Dec) and the last digit of the year, e.g. TXFH6.
_FUTURES_CONTRACT_RE = re.compile(r"^[A-Z]{3}[A-L]\d$")


# --------------------------------------------------------------------------- config
@dataclass
class CollectTargets:
    stock_codes: List[str] = field(default_factory=list)
    futures_products: List[str] = field(default_factory=list)
    futures_contracts: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.stock_codes or self.futures_products or self.futures_contracts)


def _append_unique(items: List[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def parse_collect_symbols(entries: Iterable[Tuple[str, str]]) -> CollectTargets:
    """Turn (market, symbol) pairs into stock codes and futures products / contracts.

    ``TWStock`` symbols use the sidecar code (``2330.TW`` -> ``2330``). ``TWFutures``
    symbols are a product (``TXF``, collected as near and next month) or an explicit
    contract (``TXFH6``). Anything else is ignored with a log line.
    """
    targets = CollectTargets()
    for market, raw_symbol in entries:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        if market == MARKET_STOCK:
            code = normalize_tw_symbol(symbol)
            if code:
                _append_unique(targets.stock_codes, code)
                continue
        elif market == MARKET_FUTURES:
            if _FUTURES_PRODUCT_RE.match(symbol):
                _append_unique(targets.futures_products, symbol)
                continue
            if _FUTURES_CONTRACT_RE.match(symbol):
                _append_unique(targets.futures_contracts, symbol)
                continue
        logger.info("TW K-line collection: ignored unsupported symbol %s:%s", market, raw_symbol)
    return targets


def configured_symbol_entries(raw: Optional[str] = None) -> List[Tuple[str, str]]:
    """``TW_KLINE_COLLECT_SYMBOLS`` as (market, symbol) pairs; ``Market:Symbol`` CSV."""
    text = os.getenv("TW_KLINE_COLLECT_SYMBOLS", DEFAULT_COLLECT_SYMBOLS) if raw is None else raw
    entries: List[Tuple[str, str]] = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            market, symbol = item.split(":", 1)
            market = market.strip()
        else:
            symbol = item
            market = MARKET_STOCK if normalize_tw_symbol(item) else MARKET_FUTURES
        entries.append((market, symbol.strip()))
    return entries


# --------------------------------------------------------------------------- contracts
def _contract_date(row: Dict[str, Any]) -> Optional[date]:
    for key in ("settlementDate", "endDate"):
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
    as_of: date,
    *,
    count: int = 2,
) -> List[str]:
    """Return the ``count`` nearest contracts of ``product`` still trading on ``as_of``.

    A contract trades through its settlement date's day session, so contracts with
    ``settlementDate >= as_of`` qualify; ordered by settlement date this yields the
    near month followed by the next month. Spreads and other products are ignored.
    """
    product = str(product or "").strip().upper()
    candidates: List[Tuple[date, str]] = []
    for row in tickers:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol.startswith(product) or not _FUTURES_CONTRACT_RE.match(symbol):
            continue
        settles = _contract_date(row)
        if settles is None or settles < as_of:
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


# --------------------------------------------------------------------------- schedule
@dataclass(frozen=True)
class SessionWindow:
    """One collectable session of a trading day."""

    market: str
    session: str
    trade_date: date
    ready_at: datetime
    expires_at: datetime

    def bar_range(self) -> Tuple[int, int]:
        """[start, end) unix seconds of the bars that belong to this session."""
        return session_bar_range(self.market, self.session, self.trade_date)

    def contracts_as_of(self) -> date:
        """Reference day for the near-month choice (the night session trades the next day's contracts)."""
        if self.session == SESSION_AFTERHOURS:
            return self.trade_date + timedelta(days=1)
        return self.trade_date


def _taipei(day: date, clock: dtime) -> datetime:
    return datetime.combine(day, clock, tzinfo=TAIPEI_TZ)


def _taipei_ts(day: date, clock: dtime) -> int:
    return int(_taipei(day, clock).timestamp())


def session_bar_range(market: str, session: str, day: date) -> Tuple[int, int]:
    """[start, end) unix seconds of the bars of ``session`` on trading day ``day``."""
    if market == MARKET_STOCK:
        start, end = _STOCK_WINDOW
        return _taipei_ts(day, start), _taipei_ts(day, end)
    if session == SESSION_REGULAR:
        start, end = _FUTURES_REGULAR_WINDOW
        return _taipei_ts(day, start), _taipei_ts(day, end)
    return (
        _taipei_ts(day, _FUTURES_AFTERHOURS_START),
        _taipei_ts(day + timedelta(days=1), _FUTURES_AFTERHOURS_END),
    )


def xtai_sessions(start: date, end: date) -> List[date]:
    """XTAI trading days in [start, end] (``exchange_calendars``)."""
    from app.services.market_schedule import _calendar

    calendar = _calendar(MARKET_STOCK)
    sessions = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [pd.Timestamp(s).date() for s in sessions]


def session_windows(
    now: datetime,
    *,
    sessions_lookup: Callable[[date, date], List[date]] = xtai_sessions,
    include_stock: bool = True,
    include_futures: bool = True,
) -> List[SessionWindow]:
    """Session windows whose bars are ready at ``now`` (expired ones included)."""
    current = now.astimezone(TAIPEI_TZ)
    today = current.date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    # Look ahead so the retry deadline can be placed on a following trading day.
    all_sessions = sessions_lookup(start, today + timedelta(days=30))
    windows: List[SessionWindow] = []
    for index, day in enumerate(all_sessions):
        if day < start or day > today:
            continue
        following = all_sessions[index + 1:]
        next_session = following[0] if following else day + timedelta(days=1)
        if include_stock:
            nth = following[STOCK_RETRY_SESSIONS - 1] if len(following) >= STOCK_RETRY_SESSIONS else (
                day + timedelta(days=STOCK_RETRY_SESSIONS)
            )
            windows.append(SessionWindow(
                MARKET_STOCK, SESSION_REGULAR, day, _taipei(day, STOCK_READY), _taipei(nth, STOCK_EXPIRE),
            ))
        if include_futures:
            expires = _taipei(next_session, FUTURES_EXPIRE)
            windows.append(SessionWindow(
                MARKET_FUTURES, SESSION_REGULAR, day, _taipei(day, FUTURES_REGULAR_READY), expires,
            ))
            windows.append(SessionWindow(
                MARKET_FUTURES, SESSION_AFTERHOURS, day,
                _taipei(day + timedelta(days=1), FUTURES_AFTERHOURS_READY), expires,
            ))
    return [window for window in windows if window.ready_at <= current]


# --------------------------------------------------------------------------- run
@dataclass
class CollectionReport:
    collected: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    missed: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"collected": self.collected, "failed": self.failed, "missed": self.missed}


def _entry(market: str, symbol: str, session: str, trade_date: date, **extra: Any) -> Dict[str, Any]:
    return {"market": market, "symbol": symbol, "session": session, "trade_date": trade_date.isoformat(), **extra}


def _taipei_day_of(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=TAIPEI_TZ).date()


class TWKlineCollector:
    """One collection tick: work out pending sessions, fetch, store, record status."""

    def __init__(
        self,
        *,
        source: Any = None,
        repository: Any = None,
        sessions_lookup: Callable[[date, date], List[date]] = xtai_sessions,
    ) -> None:
        if source is None:
            from app.data_sources.tw_stock import TWStockDataSource

            source = TWStockDataSource()
        if repository is None:
            from app.services.tw_kline_store import TWKlineRepository

            repository = TWKlineRepository()
        self.source = source
        self.repository = repository
        self.sessions_lookup = sessions_lookup

    # -------------------------------------------------------------- targets
    def resolve_targets(self, configured: Optional[List[Tuple[str, str]]] = None) -> CollectTargets:
        entries = list(configured if configured is not None else configured_symbol_entries())
        try:
            entries.extend(self.repository.watchlist_symbols())
        except Exception as exc:
            logger.warning("TW K-line collection: watchlist symbols unavailable: %s", exc)
        return parse_collect_symbols(entries)

    # -------------------------------------------------------------- run
    def run(self, now: Optional[datetime] = None, targets: Optional[CollectTargets] = None) -> Dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        targets = targets if targets is not None else self.resolve_targets()
        if targets.is_empty():
            return {"skipped": "no symbols configured"}
        has_futures = bool(targets.futures_products or targets.futures_contracts)
        windows = session_windows(
            current,
            sessions_lookup=self.sessions_lookup,
            include_stock=bool(targets.stock_codes),
            include_futures=has_futures,
        )
        if not windows:
            return {"skipped": "no trading session ready"}

        since = min(window.trade_date for window in windows)
        statuses = self.repository.load_statuses(since)
        report = CollectionReport()

        stock_due: Dict[str, List[SessionWindow]] = {}
        futures_due: List[Tuple[str, SessionWindow]] = []
        tickers: Optional[List[Dict[str, Any]]] = None
        tickers_error = ""

        for window in windows:
            if window.market == MARKET_STOCK:
                symbols = list(targets.stock_codes)
            else:
                symbols = list(targets.futures_contracts)
                if targets.futures_products:
                    if tickers is None and not tickers_error:
                        try:
                            tickers = self.source.get_futopt_tickers("FUTURE")
                        except FubonSidecarError as exc:
                            tickers_error = str(exc)
                            logger.warning("TW K-line collection: futures tickers unavailable: %s", exc)
                    for product in targets.futures_products:
                        if tickers is None:
                            self._fail_or_miss(
                                report, statuses, current, window, product,
                                f"futures tickers unavailable: {tickers_error}",
                            )
                            continue
                        contracts = near_month_contracts(tickers, product, window.contracts_as_of())
                        if not contracts:
                            self._fail_or_miss(
                                report, statuses, current, window, product, "no near-month contract found",
                            )
                        for contract in contracts:
                            _append_unique(symbols, contract)
            for symbol in symbols:
                key = (window.market, symbol, window.session, window.trade_date)
                if statuses.get(key) in (STATUS_SUCCESS, STATUS_MISSED):
                    continue
                if current >= window.expires_at:
                    self._mark_missed(report, window, symbol, "not collected before the Fubon data expired")
                    continue
                if window.market == MARKET_STOCK:
                    stock_due.setdefault(symbol, []).append(window)
                else:
                    futures_due.append((symbol, window))

        for code, code_windows in stock_due.items():
            self._collect_stock(report, code, code_windows)
        for contract, window in futures_due:
            self._collect_futures(report, contract, window)
        return report.as_dict()

    # -------------------------------------------------------------- helpers
    def _fail_or_miss(
        self,
        report: CollectionReport,
        statuses: Dict[Any, str],
        current: datetime,
        window: SessionWindow,
        symbol: str,
        error: str,
    ) -> None:
        key = (window.market, symbol, window.session, window.trade_date)
        if statuses.get(key) in (STATUS_SUCCESS, STATUS_MISSED):
            return
        if current >= window.expires_at:
            self._mark_missed(report, window, symbol, error)
        else:
            self._mark_failed(report, window, symbol, error)

    def _mark_failed(self, report: CollectionReport, window: SessionWindow, symbol: str, error: str) -> None:
        logger.warning(
            "TW K-line collection failed for %s:%s %s %s (retried next run): %s",
            window.market, symbol, window.session, window.trade_date, error,
        )
        self.repository.record_status(
            market=window.market, symbol=symbol, session=window.session,
            trade_date=window.trade_date, status=STATUS_FAILED, error=error,
        )
        report.failed.append(_entry(window.market, symbol, window.session, window.trade_date, error=error))

    def _mark_missed(self, report: CollectionReport, window: SessionWindow, symbol: str, error: str) -> None:
        logger.warning(
            "TW K-line gap: %s:%s %s %s was not collected and is no longer available from Fubon: %s",
            window.market, symbol, window.session, window.trade_date, error,
        )
        self.repository.record_status(
            market=window.market, symbol=symbol, session=window.session,
            trade_date=window.trade_date, status=STATUS_MISSED, error=error, attempted=False,
        )
        report.missed.append(_entry(window.market, symbol, window.session, window.trade_date, error=error))

    def _mark_success(self, report: CollectionReport, window: SessionWindow, symbol: str, count: int) -> None:
        self.repository.record_status(
            market=window.market, symbol=symbol, session=window.session,
            trade_date=window.trade_date, status=STATUS_SUCCESS, bar_count=count,
        )
        report.collected.append(_entry(window.market, symbol, window.session, window.trade_date, bars=count))

    def _collect_stock(self, report: CollectionReport, code: str, windows: List[SessionWindow]) -> None:
        try:
            bars = self.source.fetch_minute_bars(code, include_intraday=True)
        except FubonSidecarError as exc:
            for window in windows:
                self._mark_failed(report, window, code, str(exc))
            return
        in_session: List[Dict[str, Any]] = []
        counts: Dict[date, int] = {}
        for bar in bars:
            ts = int(bar["time"])
            day = _taipei_day_of(ts)
            start, end = session_bar_range(MARKET_STOCK, SESSION_REGULAR, day)
            if start <= ts < end:
                in_session.append(bar)
                counts[day] = counts.get(day, 0) + 1
        try:
            self.repository.upsert_bars(
                market=MARKET_STOCK, symbol=code, session=SESSION_REGULAR,
                timeframe="1m", bars=in_session, source=SOURCE_FUBON,
            )
        except Exception as exc:
            for window in windows:
                self._mark_failed(report, window, code, f"database write failed: {exc}")
            return
        for window in windows:
            count = counts.get(window.trade_date, 0)
            if count:
                self._mark_success(report, window, code, count)
            else:
                self._mark_failed(report, window, code, "Fubon returned no 1m bars for this trading day")

    def _collect_futures(self, report: CollectionReport, contract: str, window: SessionWindow) -> None:
        try:
            bars = self.source.fetch_futopt_minute_bars(contract, window.session)
        except FubonSidecarError as exc:
            self._mark_failed(report, window, contract, str(exc))
            return
        start, end = window.bar_range()
        in_session = [bar for bar in bars if start <= int(bar["time"]) < end]
        if not in_session:
            self._mark_failed(report, window, contract, "Fubon returned no 1m bars for this session")
            return
        try:
            self.repository.upsert_bars(
                market=MARKET_FUTURES, symbol=contract, session=window.session,
                timeframe="1m", bars=in_session, source=SOURCE_FUBON,
            )
        except Exception as exc:
            self._mark_failed(report, window, contract, f"database write failed: {exc}")
            return
        self._mark_success(report, window, contract, len(in_session))


def collection_enabled() -> bool:
    return os.getenv("TW_KLINE_COLLECT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def run_tw_kline_collection(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Entry point of the Celery Beat task; skips quietly when the sidecar is not configured."""
    if not collection_enabled():
        return {"skipped": "TW_KLINE_COLLECT_ENABLED is off"}
    from app.config.data_sources import FubonSidecarConfig

    if not FubonSidecarConfig.CONFIGURED:
        return {"skipped": "Fubon sidecar is not configured"}
    result = TWKlineCollector().run(now)
    if result.get("failed") or result.get("missed"):
        logger.warning(
            "TW K-line collection: %d collected, %d failed, %d missed",
            len(result.get("collected") or []), len(result.get("failed") or []), len(result.get("missed") or []),
        )
    return result
