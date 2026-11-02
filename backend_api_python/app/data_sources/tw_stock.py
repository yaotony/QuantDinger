"""
Taiwan stock (TWSE / TPEx) and index data source backed by the Fubon sidecar.

Read-only data source for research, backtesting and paper trading. All data comes
from the Fubon market data sidecar (``fubon_sidecar/``) over its token-protected
v1 HTTP contract; this process never talks to Fubon directly and never places
orders.

Symbol format (QuantDinger identity -> sidecar code):
    2330.TW / 0050.TW   (TWSE listed stock / ETF)  -> 2330 / 0050
    6488.TWO            (TPEx listed stock)        -> 6488
    IX0001 / IX0043     (TAIEX / TPEx index)       -> unchanged

Timeframes:
    1D / 1W        -> /v1/stock/candles  D / W (a from/to range of at most one year)
    minute / 1H/4H -> 1m bars resampled to the requested interval. The 1m bars come
                      from ``qd_tw_klines`` (accumulated daily by
                      ``app.services.tw_kline_collector``) plus the sidecar's latest
                      5 trading days and today's intraday bars; for the same bar
                      time the sidecar bar wins, so the seam has no duplicates.

Sidecar failures raise :class:`FubonSidecarError` with an operator-readable
message; ``DataSourceFactory`` turns that into a structured market-data failure
instead of an unhandled 500. Minute requests fall back to the stored bars when
the sidecar is down and the database has data for the window.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from app.config.data_sources import FubonSidecarConfig
from app.data_sources.base import BaseDataSource, TIMEFRAME_SECONDS
from app.data_sources.errors import DataSourceError
from app.utils.logger import get_logger

logger = get_logger(__name__)


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TOKEN_HEADER = "X-Sidecar-Token"

# Fubon accepts a historical D/W range of at most one year per request.
MAX_HISTORY_DAYS = 365

# The sidecar serves 1m bars for the latest 5 trading days only. Requests that end
# earlier than this many calendar days ago (long holidays included) are served from
# the database alone.
SIDECAR_MINUTE_WINDOW_DAYS = 14

# Stored bars: market / session / timeframe of the TWStock rows in qd_tw_klines.
STORE_MARKET = "TWStock"
STORE_SESSION = "regular"
STORE_TIMEFRAME = "1m"

# QuantDinger timeframe -> (native sidecar timeframe, not served natively by Fubon).
# Only D / W are requested as-is; every intraday timeframe is resampled from 1m bars.
TIMEFRAME_MAP: Dict[str, tuple[str, bool]] = {
    "1m": ("1", False),
    "3m": ("1", True),
    "5m": ("5", False),
    "15m": ("15", False),
    "30m": ("30", False),
    "1H": ("60", False),
    "4H": ("60", True),
    "1D": ("D", False),
    "1W": ("W", False),
}

PERIOD_TIMEFRAMES = frozenset({"1D", "1W"})
INTRADAY_TIMEFRAMES = frozenset(TIMEFRAME_MAP) - PERIOD_TIMEFRAMES

_TIMEFRAME_ALIASES = {key.lower(): key for key in TIMEFRAME_MAP}

# Stock / ETF codes: four digits optionally followed by up to two digits or
# letters (e.g. 2330, 00878, 00631L). Index codes: IX followed by four digits.
_STOCK_CODE_RE = re.compile(r"^\d{4}[0-9A-Z]{0,2}$")
_INDEX_CODE_RE = re.compile(r"^IX\d{4}$")
_SUFFIXES = (".TWO", ".TW")


class FubonSidecarError(DataSourceError):
    """The Fubon sidecar is unreachable, not ready, or rejected the request."""


def normalize_tw_symbol(symbol: str) -> str:
    """Return the sidecar code for a TWStock symbol, or "" when invalid.

    ``2330.TW`` -> ``2330``, ``6488.two`` -> ``6488``, ``IX0001`` -> ``IX0001``.
    The strict pattern also guards the sidecar query string.
    """
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    if s.startswith("TWSTOCK:"):
        s = s.split(":", 1)[1].strip()
    for suffix in _SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if _INDEX_CODE_RE.match(s) or _STOCK_CODE_RE.match(s):
        return s
    return ""


def normalize_tw_timeframe(timeframe: str) -> Optional[str]:
    raw = str(timeframe or "").strip()
    if raw in TIMEFRAME_MAP:
        return raw
    return _TIMEFRAME_ALIASES.get(raw.lower())


def _taipei_day(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=TAIPEI_TZ).date()


def _taipei_midnight_unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ).timestamp())


def _today_taipei() -> date:
    return datetime.now(tz=TAIPEI_TZ).date()


class TWStockDataSource(BaseDataSource):
    """Taiwan listed / OTC stocks and indices via the Fubon sidecar."""

    name = "TWStock/fubon-sidecar"

    def __init__(self, kline_store: Any = None) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "QuantDinger/TWStock-DataSource"})
        self._kline_store = kline_store

    @property
    def kline_store(self) -> Any:
        """``TWKlineRepository`` for stored 1m bars (created lazily)."""
        if self._kline_store is None:
            from app.services.tw_kline_store import TWKlineRepository

            self._kline_store = TWKlineRepository()
        return self._kline_store

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return normalize_tw_symbol(symbol)

    def _request(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """GET a sidecar endpoint. Returns None on 404 (no data), raises on other failures."""
        base_url = FubonSidecarConfig.BASE_URL
        token = FubonSidecarConfig.TOKEN
        if not token:
            raise FubonSidecarError(
                "Fubon market data sidecar is not configured: FUBON_SIDECAR_TOKEN is empty "
                "(service unavailable)."
            )
        url = f"{base_url}{path}"
        try:
            resp = self._session.get(
                url,
                params=params,
                headers={TOKEN_HEADER: token},
                timeout=FubonSidecarConfig.TIMEOUT,
            )
        except requests.Timeout as exc:
            raise FubonSidecarError(
                f"Fubon market data sidecar at {base_url} timed out ({type(exc).__name__})."
            ) from None
        except requests.RequestException as exc:
            raise FubonSidecarError(
                f"Fubon market data sidecar at {base_url} is unreachable (connection refused or "
                f"network error: {type(exc).__name__}). Start it with "
                "`docker compose --profile taiwan up -d fubon-marketdata`."
            ) from None

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                raise FubonSidecarError(
                    f"Fubon market data sidecar returned a non-JSON response for {path}."
                ) from None
            return payload if isinstance(payload, dict) else None

        code, message = self._error_body(resp)
        if resp.status_code == 404:
            logger.info("Fubon sidecar has no data for %s %s (%s)", path, params.get("symbol"), code)
            return None
        if resp.status_code == 401:
            raise FubonSidecarError(
                "Fubon market data sidecar rejected X-Sidecar-Token (401 unauthorized); "
                "check FUBON_SIDECAR_TOKEN."
            )
        if resp.status_code == 503:
            raise FubonSidecarError(
                "Fubon market data sidecar is not ready (503 service unavailable): "
                f"{message or code or 'not logged in yet'}."
            )
        if resp.status_code == 429:
            raise FubonSidecarError(
                f"Fubon market data sidecar rate limit reached (429): {message or code}."
            )
        if resp.status_code == 400:
            raise FubonSidecarError(
                f"Fubon market data sidecar rejected the request (400 {code}): {message}."
            )
        raise FubonSidecarError(
            f"Fubon market data sidecar error (HTTP {resp.status_code} {code}): {message}."
        )

    @staticmethod
    def _error_body(resp: requests.Response) -> tuple[str, str]:
        try:
            body = resp.json()
        except ValueError:
            return "", ""
        if not isinstance(body, dict):
            return "", ""
        return str(body.get("code") or "")[:64], str(body.get("message") or "")[:300]

    def _parse_candles(self, payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = (payload or {}).get("candles") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row["time"])
                out.append(self.format_kline(
                    timestamp=ts,
                    open_price=row.get("open") or 0,
                    high=row.get("high") or 0,
                    low=row.get("low") or 0,
                    close=row.get("close") or 0,
                    volume=row.get("volume") or 0,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("TWStock: skipped malformed candle %s: %s", row, exc)
        out.sort(key=lambda item: item["time"])
        return out

    @staticmethod
    def _resample(klines: List[Dict[str, Any]], target_seconds: int) -> List[Dict[str, Any]]:
        """Aggregate sorted finer-interval candles into ``target_seconds`` UTC buckets."""
        if not klines or target_seconds <= 0:
            return klines
        out: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        bucket_start = -1
        for k in klines:
            ts = int(k["time"])
            bucket = ts - (ts % target_seconds)
            if current is None or bucket != bucket_start:
                if current is not None:
                    out.append(current)
                current = dict(k, time=bucket)
                bucket_start = bucket
            else:
                current["high"] = max(current["high"], k["high"])
                current["low"] = min(current["low"], k["low"])
                current["close"] = k["close"]
                current["volume"] = round(current["volume"] + k["volume"], 2)
        if current is not None:
            out.append(current)
        return out

    @staticmethod
    def _period_range(
        timeframe: str,
        limit: int,
        before_time: Optional[int],
        after_time: Optional[int],
    ) -> Optional[tuple[date, date]]:
        """Return the Taipei-date (from, to) window for a D/W request, capped at one year."""
        end_day = _taipei_day(int(before_time) - 1) if before_time else _today_taipei()
        today = _today_taipei()
        if end_day > today:
            end_day = today
        if after_time is not None:
            start_day = _taipei_day(int(after_time))
        elif timeframe == "1W":
            start_day = end_day - timedelta(days=max(60, int(limit) * 7 + 14))
        else:
            start_day = end_day - timedelta(days=max(10, int(limit * 1.6) + 10))
        earliest = end_day - timedelta(days=MAX_HISTORY_DAYS)
        if start_day < earliest:
            start_day = earliest
        if start_day > end_day:
            return None
        return start_day, end_day

    def _fetch_period_candles(
        self,
        code: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int],
        after_time: Optional[int],
    ) -> List[Dict[str, Any]]:
        sidecar_tf, _ = TIMEFRAME_MAP[timeframe]
        window = self._period_range(timeframe, limit, before_time, after_time)
        if window is None:
            return []
        params: Dict[str, Any] = {
            "symbol": code,
            "timeframe": sidecar_tf,
            "from": window[0].isoformat(),
            "to": window[1].isoformat(),
        }
        return self._parse_candles(self._request("/v1/stock/candles", params))

    @staticmethod
    def _merge_by_time(*sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Union bars by open time; later sources win for the same time."""
        by_time: Dict[int, Dict[str, Any]] = {}
        for rows in sources:
            for row in rows:
                by_time[int(row["time"])] = row
        return [by_time[ts] for ts in sorted(by_time)]

    def fetch_minute_bars(self, code: str, *, include_intraday: bool = True) -> List[Dict[str, Any]]:
        """Sidecar 1m bars: the latest 5 trading days, plus today's intraday bars.

        Fubon rejects from/to for minute bars. The intraday merge is best effort:
        when only it fails, the historical bars are still returned.
        """
        klines = self._parse_candles(
            self._request("/v1/stock/candles", {"symbol": code, "timeframe": "1"})
        )
        if not include_intraday:
            return klines
        try:
            payload = self._request(
                "/v1/stock/intraday/candles", {"symbol": code, "timeframe": "1"}
            )
        except FubonSidecarError as exc:
            if not klines:
                raise
            logger.info("TWStock: intraday candles unavailable for %s: %s", code, exc)
            return klines
        return self._merge_by_time(klines, self._parse_candles(payload))

    def _stored_minutes(
        self,
        code: str,
        *,
        start_ts: Optional[int],
        end_ts: Optional[int],
        limit: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Stored 1m bars; an unavailable database degrades to the sidecar only."""
        try:
            return self.kline_store.load_bars(
                market=STORE_MARKET,
                symbol=code,
                session=STORE_SESSION,
                timeframe=STORE_TIMEFRAME,
                start_ts=start_ts,
                end_ts=end_ts,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("TWStock: stored 1m bars unavailable for %s: %s", code, exc)
            return []

    def _fetch_minute_candles(
        self,
        code: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int],
        after_time: Optional[int],
    ) -> List[Dict[str, Any]]:
        """1m bars from the database and the sidecar, merged without duplicates."""
        tf_minutes = max(1, TIMEFRAME_SECONDS[timeframe] // 60)
        if after_time is not None:
            stored = self._stored_minutes(code, start_ts=int(after_time), end_ts=before_time, limit=None)
        else:
            # Enough 1m bars for ``limit`` complete buckets plus one partial bucket.
            stored = self._stored_minutes(
                code, start_ts=None, end_ts=before_time, limit=(limit + 1) * tf_minutes
            )

        sidecar_reaches = (
            before_time is None
            or int(before_time) > time.time() - SIDECAR_MINUTE_WINDOW_DAYS * 86400
        )
        recent: List[Dict[str, Any]] = []
        if sidecar_reaches:
            try:
                recent = self.fetch_minute_bars(code, include_intraday=before_time is None)
            except FubonSidecarError as exc:
                if not stored:
                    raise
                logger.warning(
                    "TWStock: sidecar minute bars unavailable for %s, using stored bars only: %s",
                    code,
                    exc,
                )
        return self._merge_by_time(stored, recent)

    # ------------------------------------------------------------------ public
    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = self._normalize_symbol(symbol)
        if not code:
            logger.warning("TWStock: invalid symbol %r", symbol)
            return []
        tf = normalize_tw_timeframe(timeframe)
        if tf is None:
            raise DataSourceError(f"TWStock does not support timeframe {timeframe!r} (unsupported timeframe).")
        lim = max(int(limit or 300), 1)

        if tf in PERIOD_TIMEFRAMES:
            klines = self._fetch_period_candles(code, tf, lim, before_time, after_time)
        else:
            klines = self._fetch_minute_candles(code, tf, lim, before_time, after_time)
            if tf != "1m":
                klines = self._resample(klines, TIMEFRAME_SECONDS[tf])

        klines = self.filter_and_limit(
            klines,
            lim,
            before_time,
            after_time,
            truncate=(after_time is None),
        )
        self.log_result(code, klines, tf)
        return klines

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = self._normalize_symbol(symbol)
        display = str(symbol or "").strip().upper()
        if not code:
            return {"last": 0, "symbol": display}
        payload = self._request("/v1/stock/quote", {"symbol": code})
        if not payload:
            return {"last": 0, "symbol": display}
        return self._quote_to_ticker(payload, display)

    @staticmethod
    def _quote_to_ticker(payload: Dict[str, Any], display: str) -> Dict[str, Any]:
        """Sidecar quote body -> the HK-style ticker shape the market routes expect."""

        def _num(key: str) -> float:
            try:
                value = payload.get(key)
                return float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        last = _num("lastPrice")
        previous_close = _num("previousClose") or _num("referencePrice")
        change = _num("change")
        if not change and last and previous_close:
            change = round(last - previous_close, 4)
        change_pct = _num("changePercent")
        if not change_pct and previous_close:
            change_pct = round(change / previous_close * 100, 2)
        return {
            "last": last,
            "change": change,
            "changePercent": change_pct,
            "high": _num("high"),
            "low": _num("low"),
            "open": _num("open"),
            "previousClose": previous_close,
            "name": str(payload.get("name") or ""),
            "symbol": display,
        }

    def get_tickers(self, ticker_type: str, exchange: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return sidecar ``/v1/stock/tickers`` rows (``{symbol, name}``); [] when Fubon has none."""
        params: Dict[str, Any] = {"type": ticker_type}
        if exchange:
            params["exchange"] = exchange
        payload = self._request("/v1/stock/tickers", params)
        rows = (payload or {}).get("tickers") or []
        return [row for row in rows if isinstance(row, dict)]

    # ------------------------------------------------------------------ futures / options
    def get_futopt_tickers(self, ticker_type: str = "FUTURE") -> List[Dict[str, Any]]:
        """Sidecar ``/v1/futopt/tickers`` rows (symbol, name, settlementDate, ...)."""
        payload = self._request("/v1/futopt/tickers", {"type": ticker_type})
        rows = (payload or {}).get("tickers") or []
        return [row for row in rows if isinstance(row, dict)]

    def fetch_futopt_minute_bars(self, contract: str, session: str) -> List[Dict[str, Any]]:
        """Sidecar 1m bars of the current ``regular`` (day) or ``afterhours`` (night) session."""
        payload = self._request(
            "/v1/futopt/intraday/candles",
            {"symbol": contract, "timeframe": "1", "session": session},
        )
        return self._parse_candles(payload)

    def get_earliest_kline_time(self, symbol: str, timeframe: str) -> Optional[int]:
        """D/W: one year back from today (Fubon's range).

        Minute / hourly: the earlier of the first stored 1m bar and the first
        sidecar 1m bar (latest 5 trading days).
        """
        code = self._normalize_symbol(symbol)
        tf = normalize_tw_timeframe(timeframe)
        if not code or tf is None:
            return None
        if tf in PERIOD_TIMEFRAMES:
            return _taipei_midnight_unix(_today_taipei() - timedelta(days=MAX_HISTORY_DAYS))
        candidates: List[int] = []
        try:
            stored = self.kline_store.earliest_bar_time(
                market=STORE_MARKET, symbol=code, session=STORE_SESSION, timeframe=STORE_TIMEFRAME
            )
        except Exception as exc:
            logger.warning("TWStock: stored earliest 1m bar unavailable for %s: %s", code, exc)
            stored = None
        if stored is not None:
            candidates.append(int(stored))
        try:
            klines = self.fetch_minute_bars(code, include_intraday=False)
        except FubonSidecarError as exc:
            logger.info("TWStock earliest K-line lookup failed for %s: %s", code, exc)
            klines = []
        if klines:
            candidates.append(int(klines[0]["time"]))
        return min(candidates) if candidates else None
