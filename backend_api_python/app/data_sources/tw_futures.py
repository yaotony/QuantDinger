"""
Taiwan futures (TAIFEX) data source backed by stored 1m K-lines and the Fubon sidecar.

Read-only: research, backtesting and paper trading. Nothing here places orders.

Symbol format (QuantDinger identity):
    TXF / MXF / TMF / EXF / FXF   continuous near month, stitched by date
    TXFH6                         one contract month (H = August, 6 = 2026)
    TXO23000H6                    one option contract (``app.data_sources.tw_options``);
                                  quotes and K-lines only, never backtested
    TXF@regular                   internal data symbol: day session only (see below)

Sessions (Asia/Taipei):
    day   (``regular``)    08:45-13:45
    night (``afterhours``) 15:00-05:00 next calendar day. A night session belongs to
                           the next XTAI trading day (TAIFEX trading-day definition).

``session`` is ``all`` (default, day + night) or ``regular`` (day only). The backtest
service selects it per run and passes it to this provider as the ``@regular`` data
symbol suffix, so cached frames of the two variants never mix.

Data:
    1m bars come from ``qd_tw_klines`` (accumulated by ``app.services.tw_kline_collector``
    per contract and session) plus the sidecar ``/v1/futopt/intraday/candles`` for the
    current session. Every other timeframe is built from 1m bars: intraday ones by UTC
    bucket resampling; 1D groups the night session with the following day session
    (labelled at Asia/Taipei midnight of the trading day); 1W groups trading days by
    ISO week (labelled at Monday Asia/Taipei midnight). Trading days without 1m bars
    fall back to stored ``1D`` session rows imported from the TAIFEX daily report
    (``app.services.taifex_history``), which reach back years before the 1m history.

Continuous codes use the front month without price adjustment
(``ROLL_METHOD = "front_month_unadjusted"``): each instant reads the contract that was
the near month then; a contract stops being the near month at 13:30 on its last
trading day (``app.data_sources.tw_futures_contracts``), so prices jump by the
calendar spread at each roll.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.data_sources.base import TIMEFRAME_SECONDS
from app.data_sources.errors import DataSourceError
from app.data_sources.tw_futures_contracts import (
    CONTRACT_RE,
    PRODUCT_RE,
    TAIPEI_TZ,
    TradingDayCheck,
    front_month,
    front_month_segments,
    near_month_contracts,
    next_trading_day,
    xtai_is_trading_day,
)
from app.data_sources.tw_options import OPTION_RE
from app.data_sources.tw_stock import (
    INTRADAY_TIMEFRAMES,
    FubonSidecarError,
    TWStockDataSource,
    normalize_tw_timeframe,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

MARKET = "TWFutures"
STORE_TIMEFRAME = "1m"
# Daily session bars imported from the TAIFEX daily report (app.services.taifex_history).
STORE_DAILY_TIMEFRAME = "1D"
STORE_SESSION_DAY = "regular"
STORE_SESSION_NIGHT = "afterhours"

SESSION_ALL = "all"
SESSION_REGULAR = "regular"
SESSIONS = (SESSION_ALL, SESSION_REGULAR)
DEFAULT_SESSION = SESSION_ALL

ROLL_METHOD = "front_month_unadjusted"

# Bars accepted per session, Asia/Taipei wall time (generous around the official
# 08:45-13:45 and 15:00-05:00 hours, matching the collector's windows).
_DAY_WINDOW = (dtime(8, 30), dtime(14, 0))
_NIGHT_START = dtime(14, 50)
_NIGHT_END = dtime(5, 10)

# The sidecar serves only the current session's 1m bars; skip it for windows that
# ended earlier than this.
SIDECAR_RECENT_SECONDS = 2 * 86400


def normalize_session(value: Any) -> str:
    """``None`` / ``""`` -> ``all``; raise for anything other than ``all`` / ``regular``."""
    raw = str(value or "").strip().lower()
    if not raw:
        return DEFAULT_SESSION
    if raw not in SESSIONS:
        raise ValueError(f"strategyV2.twFuturesSessionUnsupported:{raw}")
    return raw


@dataclass(frozen=True)
class TWFuturesSymbol:
    product: str
    contract: str  # "" for the continuous near month
    session: str

    @property
    def continuous(self) -> bool:
        return not self.contract

    @property
    def is_option(self) -> bool:
        return bool(OPTION_RE.match(self.contract))


def parse_tw_futures_symbol(symbol: str) -> Optional[TWFuturesSymbol]:
    """``TXF`` / ``TWFutures:TXFH6`` / ``TXF@regular`` -> parsed symbol, or None when invalid."""
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    if s.startswith("TWFUTURES:"):
        s = s.split(":", 1)[1].strip()
    session = DEFAULT_SESSION
    if "@" in s:
        s, suffix = s.split("@", 1)
        try:
            session = normalize_session(suffix)
        except ValueError:
            return None
    if PRODUCT_RE.match(s):
        return TWFuturesSymbol(s, "", session)
    match = CONTRACT_RE.match(s) or OPTION_RE.match(s)
    if match:
        return TWFuturesSymbol(match.group(1), s, session)
    return None


def tw_futures_data_symbol(symbol: str, session: str) -> str:
    """Provider symbol for ``session``: ``TXF`` (all) or ``TXF@regular``."""
    base = str(symbol or "").strip().upper().split("@", 1)[0]
    return f"{base}@{SESSION_REGULAR}" if normalize_session(session) == SESSION_REGULAR else base


def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=TAIPEI_TZ)


def _midnight_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ).timestamp())


def bar_session(ts: int) -> Optional[str]:
    """``regular`` / ``afterhours`` for a 1m bar open time, None outside both sessions."""
    clock = _local(ts).time()
    if _DAY_WINDOW[0] <= clock < _DAY_WINDOW[1]:
        return STORE_SESSION_DAY
    if clock >= _NIGHT_START or clock < _NIGHT_END:
        return STORE_SESSION_NIGHT
    return None


def trading_day_of(ts: int, is_trading_day: Optional[TradingDayCheck] = None) -> Optional[date]:
    """TAIFEX trading day of a bar: the day session's date, or the day after a night session."""
    local = _local(ts)
    session = bar_session(ts)
    if session == STORE_SESSION_DAY:
        return local.date()
    if session == STORE_SESSION_NIGHT:
        opened = local.date() if local.time() >= _NIGHT_START else local.date() - timedelta(days=1)
        return next_trading_day(opened, is_trading_day)
    return None


def aggregate_daily(
    minutes: List[Dict[str, Any]],
    is_trading_day: Optional[TradingDayCheck] = None,
) -> List[Dict[str, Any]]:
    """Group sorted 1m bars into trading-day bars labelled at Asia/Taipei midnight."""
    by_day: Dict[date, Dict[str, Any]] = {}
    for bar in minutes:
        day = trading_day_of(int(bar["time"]), is_trading_day)
        if day is None:
            continue
        current = by_day.get(day)
        if current is None:
            by_day[day] = dict(bar, time=_midnight_ts(day))
            continue
        current["high"] = max(current["high"], bar["high"])
        current["low"] = min(current["low"], bar["low"])
        current["close"] = bar["close"]
        current["volume"] = round(current["volume"] + bar["volume"], 2)
    return [by_day[day] for day in sorted(by_day)]


def aggregate_weekly(daily: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group trading-day bars by ISO week, labelled at Monday Asia/Taipei midnight."""
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for bar in daily:
        day = _local(int(bar["time"])).date()
        monday = day - timedelta(days=day.weekday())
        label = _midnight_ts(monday)
        if current is None or current["time"] != label:
            if current is not None:
                out.append(current)
            current = dict(bar, time=label)
            continue
        current["high"] = max(current["high"], bar["high"])
        current["low"] = min(current["low"], bar["low"])
        current["close"] = bar["close"]
        current["volume"] = round(current["volume"] + bar["volume"], 2)
    if current is not None:
        out.append(current)
    return out


class TWFuturesDataSource(TWStockDataSource):
    """TAIFEX futures: stored 1m history plus the sidecar's current session."""

    name = "TWFutures/fubon-sidecar"

    def __init__(
        self,
        kline_store: Any = None,
        *,
        is_trading_day: Optional[TradingDayCheck] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(kline_store=kline_store)
        self._session.headers.update({"User-Agent": "QuantDinger/TWFutures-DataSource"})
        self._is_trading_day = is_trading_day or xtai_is_trading_day
        self._clock = clock or time.time

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _store_sessions(session: str) -> Tuple[str, ...]:
        if session == SESSION_REGULAR:
            return (STORE_SESSION_DAY,)
        return (STORE_SESSION_NIGHT, STORE_SESSION_DAY)

    def _segments(self, parsed: TWFuturesSymbol, start_ts: int, end_ts: int) -> List[Tuple[str, int, int]]:
        if parsed.continuous:
            return front_month_segments(parsed.product, start_ts, end_ts, self._is_trading_day)
        return [(parsed.contract, int(start_ts), int(end_ts))]

    def _stored_contract_minutes(self, contract: str, session: str, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        return self.kline_store.load_bars(
            market=MARKET,
            symbol=contract,
            session=session,
            timeframe=STORE_TIMEFRAME,
            start_ts=start_ts,
            end_ts=end_ts,
        )

    def _current_contract(self, parsed: TWFuturesSymbol, now_ts: int) -> str:
        if parsed.contract:
            return parsed.contract
        return front_month(parsed.product, _local(now_ts), self._is_trading_day).code

    def _load_minutes(self, parsed: TWFuturesSymbol, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """1m bars in ``[start_ts, end_ts)`` for the chosen sessions, stitched by near month."""
        sessions = self._store_sessions(parsed.session)
        segments = self._segments(parsed, start_ts, end_ts)
        stored: List[Dict[str, Any]] = []
        db_error: Optional[Exception] = None
        for contract, seg_start, seg_end in segments:
            for session in sessions:
                try:
                    rows = self._stored_contract_minutes(contract, session, seg_start, seg_end)
                except Exception as exc:  # database unavailable: degrade to the sidecar
                    db_error = exc
                    rows = []
                stored.extend(rows)
        if db_error is not None:
            logger.warning("TWFutures: stored 1m bars unavailable for %s: %s", parsed.product, db_error)

        now_ts = int(self._clock())
        recent: List[Dict[str, Any]] = []
        if end_ts > now_ts - SIDECAR_RECENT_SECONDS:
            contract = self._current_contract(parsed, now_ts)
            # The sidecar contract only counts inside its own near-month piece.
            pieces = [(s, e) for c, s, e in segments if c == contract]
            errors: List[str] = []
            for session in sessions:
                try:
                    rows = self.fetch_futopt_minute_bars(contract, session)
                except FubonSidecarError as exc:
                    errors.append(str(exc))
                    continue
                recent.extend(
                    row for row in rows
                    if any(s <= int(row["time"]) < e for s, e in pieces)
                )
            if errors and len(errors) == len(sessions):
                if not stored:
                    raise FubonSidecarError(errors[0])
                logger.warning(
                    "TWFutures: sidecar intraday bars unavailable for %s, using stored bars only: %s",
                    contract,
                    errors[0],
                )

        merged = self._merge_by_time(stored, recent)
        allowed = set(sessions)
        return [
            row for row in merged
            if start_ts <= int(row["time"]) < end_ts and bar_session(int(row["time"])) in allowed
        ]

    def _load_stored_daily(self, parsed: TWFuturesSymbol, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """Trading-day bars from stored ``1D`` session rows (TAIFEX daily report imports).

        Each row is labelled at Asia/Taipei midnight of its trading day, so the
        near-month piece containing that label picks the contract. A day's night and
        day rows are combined like :func:`aggregate_daily` (night open, day close).
        """
        sessions = self._store_sessions(parsed.session)  # night first, then day
        by_day: Dict[int, Dict[str, Any]] = {}
        for contract, seg_start, seg_end in self._segments(parsed, start_ts, end_ts):
            for session in sessions:
                try:
                    rows = self.kline_store.load_bars(
                        market=MARKET, symbol=contract, session=session,
                        timeframe=STORE_DAILY_TIMEFRAME, start_ts=seg_start, end_ts=seg_end,
                    )
                except Exception as exc:  # database unavailable: minute data only
                    logger.warning("TWFutures: stored daily bars unavailable for %s: %s", contract, exc)
                    return []
                for row in rows:
                    label = int(row["time"])
                    current = by_day.get(label)
                    if current is None:
                        by_day[label] = dict(row)
                        continue
                    current["high"] = max(current["high"], row["high"])
                    current["low"] = min(current["low"], row["low"])
                    current["close"] = row["close"]
                    current["volume"] = round(current["volume"] + row["volume"], 2)
        return [by_day[label] for label in sorted(by_day)]

    def _daily_bars(self, parsed: TWFuturesSymbol, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """Trading-day bars: from 1m bars where present, else from stored daily rows."""
        stored_daily = self._load_stored_daily(parsed, start_ts, end_ts)
        try:
            minutes = self._load_minutes(parsed, start_ts, end_ts)
        except FubonSidecarError:
            if not stored_daily:
                raise
            minutes = []
        from_minutes = aggregate_daily(minutes, self._is_trading_day)
        covered = {int(bar["time"]) for bar in from_minutes}
        merged = from_minutes + [bar for bar in stored_daily if int(bar["time"]) not in covered]
        merged.sort(key=lambda bar: int(bar["time"]))
        return merged

    def _window(
        self,
        tf: str,
        limit: int,
        session: str,
        before_time: Optional[int],
        after_time: Optional[int],
    ) -> Tuple[int, int]:
        end_ts = int(before_time) if before_time else int(self._clock()) + 60
        if after_time is not None:
            start_ts = int(after_time)
        elif tf == "1W":
            start_ts = end_ts - (limit * 7 + 14) * 86400
        elif tf == "1D":
            start_ts = end_ts - (int(limit * 1.6) + 10) * 86400
        else:
            # Calendar span per trading second: about 1.5x for day + night, about
            # 7x for the five-hour day session alone; plus a long-weekend margin.
            ratio = 7 if session == SESSION_REGULAR else 2
            start_ts = end_ts - (limit + 1) * TIMEFRAME_SECONDS[tf] * ratio - 5 * 86400
        if tf in ("1D", "1W"):
            # A trading day starts with the previous evening's night session.
            start_ts -= 5 * 86400
        return start_ts, end_ts

    # ------------------------------------------------------------------ public
    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        parsed = parse_tw_futures_symbol(symbol)
        if parsed is None:
            logger.warning("TWFutures: invalid symbol %r", symbol)
            return []
        tf = normalize_tw_timeframe(timeframe)
        if tf is None:
            raise DataSourceError(f"TWFutures does not support timeframe {timeframe!r} (unsupported timeframe).")
        lim = max(int(limit or 300), 1)

        start_ts, end_ts = self._window(tf, lim, parsed.session, before_time, after_time)
        if tf == "1m":
            klines = self._load_minutes(parsed, start_ts, end_ts)
        elif tf in INTRADAY_TIMEFRAMES:
            klines = self._resample(self._load_minutes(parsed, start_ts, end_ts), TIMEFRAME_SECONDS[tf])
        else:
            klines = self._daily_bars(parsed, start_ts, end_ts)
            if tf == "1W":
                klines = aggregate_weekly(klines)

        klines = self.filter_and_limit(
            klines,
            lim,
            before_time,
            after_time,
            truncate=(after_time is None),
        )
        self.log_result(symbol, klines, tf)
        return klines

    def resolve_contract(self, symbol: str) -> str:
        """Contract code a symbol trades right now (continuous -> near month)."""
        parsed = parse_tw_futures_symbol(symbol)
        if parsed is None:
            return ""
        if parsed.contract:
            return parsed.contract
        now = _local(int(self._clock()))
        try:
            contracts = near_month_contracts(self.get_futopt_tickers("FUTURE"), parsed.product, now, count=1)
        except FubonSidecarError as exc:
            logger.info("TWFutures: tickers unavailable, using the calendar near month: %s", exc)
            contracts = []
        return contracts[0] if contracts else front_month(parsed.product, now, self._is_trading_day).code

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        display = str(symbol or "").strip().upper()
        contract = self.resolve_contract(symbol)
        if not contract:
            return {"last": 0, "symbol": display}
        payload = self._request("/v1/futopt/quote", {"symbol": contract})
        if not payload:
            return {"last": 0, "symbol": display, "contract": contract}
        ticker = self._quote_to_ticker(payload, display)
        ticker["contract"] = contract
        return ticker

    def get_earliest_kline_time(self, symbol: str, timeframe: str) -> Optional[int]:
        """First stored 1m bar of the contract (or of any month for a continuous code).

        Daily / weekly: Asia/Taipei midnight of that bar's trading day, or the first
        stored daily bar (TAIFEX daily report import) when that is earlier.
        """
        parsed = parse_tw_futures_symbol(symbol)
        tf = normalize_tw_timeframe(timeframe)
        if parsed is None or tf is None:
            return None
        pattern = parsed.contract or f"{parsed.product}__"
        sessions = self._store_sessions(parsed.session)
        try:
            earliest = self.kline_store.earliest_bar_time_like(
                market=MARKET,
                symbol_pattern=pattern,
                sessions=sessions,
                timeframe=STORE_TIMEFRAME,
            )
            earliest_daily = None
            if tf in ("1D", "1W"):
                earliest_daily = self.kline_store.earliest_bar_time_like(
                    market=MARKET,
                    symbol_pattern=pattern,
                    sessions=sessions,
                    timeframe=STORE_DAILY_TIMEFRAME,
                )
        except Exception as exc:
            logger.warning("TWFutures: stored earliest bar unavailable for %s: %s", symbol, exc)
            return None
        if tf not in ("1D", "1W"):
            return int(earliest) if earliest is not None else None
        candidates: List[int] = []
        if earliest is not None:
            day = trading_day_of(int(earliest), self._is_trading_day)
            candidates.append(_midnight_ts(day) if day is not None else int(earliest))
        if earliest_daily is not None:
            candidates.append(int(earliest_daily))
        return min(candidates) if candidates else None
