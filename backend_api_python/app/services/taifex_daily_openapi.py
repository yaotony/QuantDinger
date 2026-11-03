"""Daily ``TWFutures`` 1D bars from the TAIFEX OpenAPI ``DailyMarketReportFut``.

Replaces the daily ``futDataDown`` web download of :mod:`app.services.taifex_history`
for the day-to-day accumulation. The endpoint has no parameters and only serves
the latest trading day (one row per contract month and session), so a day that
is not collected while it is published can no longer be fetched from the
OpenAPI; such days are recorded as ``missed`` (the multi-year ``futDataDown``
import remains the way to fill older history).

Row rules are the ones of :func:`taifex_history.parse_daily_report`:

* ``Contract`` is mapped with :data:`taifex_history.TAIFEX_TO_FUBON`
  (``TX`` -> ``TXF``); only the requested products are kept.
* Weekly contracts and spreads (a ``/`` in ``ContractMonth(Week)`` or
  ``Contract``) are skipped.
* ``TradingSession`` ``一般`` is trading day D (``regular``); ``盤後`` is the
  night session that opened at 15:00 on D and belongs to the next XTAI trading
  day (``afterhours``). Bars are labelled at Asia/Taipei midnight of their
  trading day.
* ``Open`` / ``High`` / ``Low`` / ``Last`` (close) / ``Volume``; ``"NULL"``,
  ``"-"`` and blanks are missing values and a row missing any of the four
  prices is not written. A missing volume is stored as 0.

The ``盤後`` attribution is the single switch
:data:`AFTERHOURS_ROW_OPENS_ON_REPORT_DATE`. The OpenAPI client investigation
(see :mod:`app.services.taifex_openapi`) found that a ``盤後`` row of ``Date=D``
may instead be the night session belonging to trading day D; until that is
confirmed against ``TimeAndSalesData`` the ``futDataDown`` rule is kept so both
sources label the same session identically.

Bars are written through ``TWKlineRepository.upsert_bars`` with
``source=taifex``: idempotent, and never replacing a ``source=fubon`` bar. Each
report date's outcome is recorded in ``qd_tw_kline_collect_status`` under
``(TWFutures, TAIFEX_DAILY, all, day)``. The status symbol is not a contract
code so it never collides with the Fubon 1m collection status of a contract.

Scheduling: the Celery Beat entry ``taifex-daily-collect``
(``quantdinger.tasks.taifex_daily_collect``, maintenance queue) calls
:func:`run_scheduled_daily_collect` every ``TAIFEX_DAILY_COLLECT_INTERVAL_SEC``
(default 1800 s). It does nothing before ``TAIFEX_DAILY_COLLECT_NOT_BEFORE``
(default 07:30 Asia/Taipei; the report is published at about 06:35-06:40) and
costs one status query once every expected day is collected, so the interval
doubles as the retry cadence while a report is late. After a run that had
pending days, :func:`check_freshness` compares the latest ``source=taifex`` 1D
day-session bar in ``qd_tw_klines`` with the latest due XTAI trading day and
logs a warning when it is behind; the per-day outcome stays in
``qd_tw_kline_collect_status``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.data_sources.tw_futures_contracts import (
    TAIPEI_TZ,
    TradingDayCheck,
    next_trading_day,
    xtai_is_trading_day,
)
from app.services.taifex_history import (
    DEFAULT_PRODUCTS,
    TIMEFRAME_DAILY,
    DailyBar,
    contract_symbol,
    parse_products,
)
from app.services.taifex_openapi import TaifexOpenApiClient, parse_number, parse_row_date
from app.services.tw_kline_store import (
    MARKET_FUTURES,
    SESSION_AFTERHOURS,
    SESSION_REGULAR,
    SOURCE_TAIFEX,
    STATUS_FAILED,
    STATUS_MISSED,
    STATUS_SUCCESS,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

ENDPOINT = "DailyMarketReportFut"
STATUS_SYMBOL = "TAIFEX_DAILY"
STATUS_SESSION = "all"
# Trading days (before today) whose report is expected.
LOOKBACK_TRADING_DAYS = 5

# True: a ``盤後`` row of report date D opened at 15:00 on D and belongs to the
# next trading day (the ``futDataDown`` rule). See the module docstring.
AFTERHOURS_ROW_OPENS_ON_REPORT_DATE = True

_SESSIONS = {"一般": SESSION_REGULAR, "盤後": SESSION_AFTERHOURS}
_PRICE_FIELDS = ("Open", "High", "Low", "Last")


# --------------------------------------------------------------------------- parsing
def parse_openapi_daily_row(
    row: Dict[str, Any],
    *,
    wanted: Optional[set] = None,
    is_trading_day: Optional[TradingDayCheck] = None,
    afterhours_opens_on_report_date: bool = AFTERHOURS_ROW_OPENS_ON_REPORT_DATE,
) -> Optional[DailyBar]:
    """One ``DailyMarketReportFut`` object -> :class:`DailyBar`, or None when skipped."""
    code = str(row.get("Contract") or "").strip().upper()
    if wanted and code not in wanted:
        return None
    symbol = contract_symbol(code, str(row.get("ContractMonth(Week)") or ""))
    session = _SESSIONS.get(str(row.get("TradingSession") or "").strip() or "一般")
    if symbol is None or session is None:
        return None
    prices = [parse_number(row.get(name)) for name in _PRICE_FIELDS]
    if any(price is None for price in prices):
        return None
    day = parse_row_date(row.get("Date"))
    if day is None:
        return None
    if session == SESSION_AFTERHOURS and afterhours_opens_on_report_date:
        trade_day = next_trading_day(day, is_trading_day or xtai_is_trading_day)
    else:
        trade_day = day
    open_, high, low, close = (float(p) for p in prices)  # type: ignore[arg-type]
    return DailyBar(symbol, session, trade_day, open_, high, low, close, parse_number(row.get("Volume")) or 0.0)


def parse_openapi_daily_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    products: Optional[Iterable[str]] = None,
    is_trading_day: Optional[TradingDayCheck] = None,
    afterhours_opens_on_report_date: bool = AFTERHOURS_ROW_OPENS_ON_REPORT_DATE,
) -> List[DailyBar]:
    """Parse ``DailyMarketReportFut`` rows (TAIFEX product codes in ``products``)."""
    wanted = set(parse_products(list(products))) if products is not None else set()
    check = is_trading_day or xtai_is_trading_day
    out: List[DailyBar] = []
    for row in rows:
        bar = parse_openapi_daily_row(
            row, wanted=wanted, is_trading_day=check,
            afterhours_opens_on_report_date=afterhours_opens_on_report_date,
        )
        if bar is not None:
            out.append(bar)
    return out


# --------------------------------------------------------------------------- collection
@dataclass
class DailyCollectResult:
    data_date: Optional[date] = None
    written: Dict[str, int] = field(default_factory=dict)  # "TXFH6/regular" -> bars
    collected: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    missed: List[Dict[str, Any]] = field(default_factory=list)
    skipped: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "data_date": self.data_date.isoformat() if self.data_date else None,
            "written": dict(self.written),
            "collected": list(self.collected),
            "failed": list(self.failed),
            "missed": list(self.missed),
        }
        if self.skipped:
            out["skipped"] = self.skipped
        return out


def expected_days(today: date, is_trading_day: TradingDayCheck, count: int = LOOKBACK_TRADING_DAYS) -> List[date]:
    """The ``count`` most recent trading days strictly before ``today``, oldest first.

    A trading day's report is published the next morning, so today's is never due.
    """
    out: List[date] = []
    cursor = today - timedelta(days=1)
    for _ in range(count * 4 + 14):
        if len(out) >= count:
            break
        if is_trading_day(cursor):
            out.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(out)


class TaifexDailyOpenApiCollector:
    """Download ``DailyMarketReportFut`` and store its 1D bars for the configured products."""

    def __init__(
        self,
        *,
        client: Any = None,
        repository: Any = None,
        products: Optional[Sequence[str]] = None,
        is_trading_day: Optional[TradingDayCheck] = None,
        lookback: int = LOOKBACK_TRADING_DAYS,
        afterhours_opens_on_report_date: bool = AFTERHOURS_ROW_OPENS_ON_REPORT_DATE,
    ) -> None:
        if repository is None:
            from app.services.tw_kline_store import TWKlineRepository

            repository = TWKlineRepository()
        self.client = client or TaifexOpenApiClient()
        self.repository = repository
        self.products = parse_products(list(products if products is not None else DEFAULT_PRODUCTS))
        self.is_trading_day = is_trading_day or xtai_is_trading_day
        self.lookback = max(1, int(lookback))
        self.afterhours_opens_on_report_date = afterhours_opens_on_report_date

    # -------------------------------------------------------------- status
    def _record(self, result: DailyCollectResult, day: date, status: str, *, bars: int = 0,
                error: str = "", attempted: bool = True) -> None:
        self.repository.record_status(
            market=MARKET_FUTURES, symbol=STATUS_SYMBOL, session=STATUS_SESSION,
            trade_date=day, status=status, bar_count=bars, error=error, attempted=attempted,
        )
        entry: Dict[str, Any] = {"trade_date": day.isoformat()}
        if status == STATUS_SUCCESS:
            entry["bars"] = bars
            result.collected.append(entry)
        elif status == STATUS_MISSED:
            logger.warning(
                "TAIFEX daily gap: %s was not collected and the OpenAPI no longer serves it: %s", day, error,
            )
            entry["error"] = error
            result.missed.append(entry)
        else:
            logger.warning("TAIFEX daily collection failed for %s (retried next run): %s", day, error)
            entry["error"] = error[:300]
            result.failed.append(entry)

    # -------------------------------------------------------------- writes
    def _write(self, bars: List[DailyBar], result: DailyCollectResult) -> int:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for bar in bars:
            grouped.setdefault((bar.symbol, bar.session), []).append(bar.as_bar())
        total = 0
        for (symbol, session), rows in sorted(grouped.items()):
            count = self.repository.upsert_bars(
                market=MARKET_FUTURES, symbol=symbol, session=session,
                timeframe=TIMEFRAME_DAILY, bars=rows, source=SOURCE_TAIFEX,
            )
            key = f"{symbol}/{session}"
            result.written[key] = result.written.get(key, 0) + int(count)
            total += int(count)
        return total

    # -------------------------------------------------------------- run
    def run(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """One tick: collect the published report unless every expected day is done.

        * the report's date D (max ``Date`` of the rows) not yet ``success`` ->
          bars written, D recorded ``success`` (``failed`` when the write fails
          or no row of the products has prices);
        * expected days before D still not ``success`` -> ``missed``;
        * expected days after D (not published yet) -> ``failed``, retried;
        * download error or empty report -> every pending day ``failed``.
        """
        result = DailyCollectResult()
        today = (now or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ).date()
        days = expected_days(today, self.is_trading_day, self.lookback)
        if not days:
            result.skipped = "no trading day expected"
            return result.as_dict()
        statuses = self.repository.load_statuses(days[0])

        def done(day: date) -> bool:
            key = (MARKET_FUTURES, STATUS_SYMBOL, STATUS_SESSION, day)
            return statuses.get(key) in (STATUS_SUCCESS, STATUS_MISSED)

        pending = [day for day in days if not done(day)]
        if not pending:
            result.skipped = "every expected trading day already collected"
            return result.as_dict()

        try:
            response = self.client.fetch(ENDPOINT)
            bars = parse_openapi_daily_rows(
                response, products=self.products, is_trading_day=self.is_trading_day,
                afterhours_opens_on_report_date=self.afterhours_opens_on_report_date,
            )
        except Exception as exc:  # retried by the next tick
            for day in pending:
                self._record(result, day, STATUS_FAILED, error=f"{ENDPOINT} download failed: {exc}")
            return result.as_dict()

        data_date = response.data_date
        result.data_date = data_date
        if data_date is None:
            for day in pending:
                self._record(result, day, STATUS_FAILED, error=f"{ENDPOINT} returned no rows")
            return result.as_dict()

        if not done(data_date):
            if not bars:
                self._record(result, data_date, STATUS_FAILED,
                             error=f"{ENDPOINT} has no priced rows for {','.join(self.products)}")
            else:
                try:
                    total = self._write(bars, result)
                except Exception as exc:
                    self._record(result, data_date, STATUS_FAILED, error=f"database write failed: {exc}")
                else:
                    self._record(result, data_date, STATUS_SUCCESS, bars=total)

        for day in pending:
            if day < data_date:
                self._record(result, day, STATUS_MISSED, attempted=False,
                             error=f"{ENDPOINT} already serves {data_date.isoformat()}")
            elif day > data_date:
                self._record(result, day, STATUS_FAILED,
                             error=f"not published yet; {ENDPOINT} still serves {data_date.isoformat()}")
        return result.as_dict()


def run_taifex_daily_collect(
    now: Optional[datetime] = None,
    *,
    products: Optional[Sequence[str]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Entry point for a scheduler: one collection tick with default dependencies."""
    result = TaifexDailyOpenApiCollector(products=products, **kwargs).run(now)
    if result.get("failed") or result.get("missed"):
        logger.warning(
            "TAIFEX daily collection: %d collected, %d failed, %d missed",
            len(result.get("collected") or []), len(result.get("failed") or []), len(result.get("missed") or []),
        )
    return result


# --------------------------------------------------------------------------- schedule
ENABLED_ENV = "TAIFEX_DAILY_COLLECT_ENABLED"
PRODUCTS_ENV = "TAIFEX_DAILY_COLLECT_PRODUCTS"
NOT_BEFORE_ENV = "TAIFEX_DAILY_COLLECT_NOT_BEFORE"
# The report of trading day D is published at about 06:35-06:40 Asia/Taipei on
# the next morning (see app.services.taifex_openapi); start an hour later.
DEFAULT_NOT_BEFORE = dtime(7, 30)
# How far back the freshness query scans ``qd_tw_klines``.
FRESHNESS_SCAN_DAYS = 45


def _enabled() -> bool:
    return os.getenv(ENABLED_ENV, "true").strip().lower() in {"1", "true", "yes", "on"}


def configured_products(raw: Optional[str] = None) -> List[str]:
    text = os.getenv(PRODUCTS_ENV, ",".join(DEFAULT_PRODUCTS)) if raw is None else raw
    return parse_products(text)


def configured_not_before(raw: Optional[str] = None) -> dtime:
    """``TAIFEX_DAILY_COLLECT_NOT_BEFORE`` (``HH:MM`` Asia/Taipei); invalid values use 07:30."""
    text = (os.getenv(NOT_BEFORE_ENV, "") if raw is None else raw).strip()
    if not text:
        return DEFAULT_NOT_BEFORE
    try:
        return dtime.fromisoformat(text)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", NOT_BEFORE_ENV, text, DEFAULT_NOT_BEFORE.strftime("%H:%M"))
        return DEFAULT_NOT_BEFORE


def check_freshness(
    repository: Any,
    now: Optional[datetime] = None,
    *,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> Dict[str, Any]:
    """Compare the latest ``source=taifex`` 1D day-session bar with the latest due trading day.

    The due day is the most recent XTAI trading day before today (its report is
    published the next morning). Only ``regular`` bars are compared because an
    after-hours bar is labelled with the following trading day. A stale result
    is logged as a warning.
    """
    check = is_trading_day or xtai_is_trading_day
    moment = (now or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ)
    today = moment.date()
    due = expected_days(today, check, 1)
    expected = due[-1] if due else None
    latest_ts = repository.latest_bar_time(
        market=MARKET_FUTURES, session=SESSION_REGULAR, timeframe=TIMEFRAME_DAILY, source=SOURCE_TAIFEX,
        since=moment - timedelta(days=FRESHNESS_SCAN_DAYS),
    )
    latest = datetime.fromtimestamp(latest_ts, TAIPEI_TZ).date() if latest_ts is not None else None
    lag = 0
    if expected is not None and (latest is None or latest < expected):
        # Due trading days after ``latest`` (bounded by the scan window when nothing is stored).
        floor = latest or today - timedelta(days=FRESHNESS_SCAN_DAYS)
        cursor = expected
        while cursor > floor:
            lag += int(bool(check(cursor)))
            cursor -= timedelta(days=1)
    stale = lag > 0
    out: Dict[str, Any] = {
        "latest": latest.isoformat() if latest else None,
        "expected": expected.isoformat() if expected else None,
        "lag_trading_days": lag,
        "stale": stale,
    }
    if stale:
        logger.warning(
            "TAIFEX daily bars are stale: latest source=taifex 1D bar %s, expected %s (%d trading day(s) behind)",
            out["latest"] or "none", out["expected"], lag,
        )
    return out


def run_scheduled_daily_collect(now: Optional[datetime] = None, **kwargs: Any) -> Dict[str, Any]:
    """Celery Beat entry point: collect after the morning publication, then check freshness.

    Skipped when ``TAIFEX_DAILY_COLLECT_ENABLED`` is off, while the Fubon sidecar
    (the ``TWFutures`` market) is not configured, and before
    ``TAIFEX_DAILY_COLLECT_NOT_BEFORE`` Asia/Taipei.
    """
    if not _enabled():
        return {"skipped": f"{ENABLED_ENV} is off"}
    from app.config.data_sources import FubonSidecarConfig

    if not FubonSidecarConfig.CONFIGURED:
        return {"skipped": "Fubon sidecar is not configured"}
    moment = (now or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ)
    not_before = configured_not_before()
    if moment.time() < not_before:
        return {"skipped": f"before {not_before.strftime('%H:%M')} Asia/Taipei"}

    if kwargs.get("repository") is None:
        from app.services.tw_kline_store import TWKlineRepository

        kwargs["repository"] = TWKlineRepository()
    kwargs.setdefault("products", configured_products())
    result = run_taifex_daily_collect(moment, **kwargs)
    # Every expected day is already success/missed: nothing changed since the last check.
    if result.get("skipped"):
        return result
    try:
        result["freshness"] = check_freshness(
            kwargs["repository"], moment, is_trading_day=kwargs.get("is_trading_day"),
        )
    except Exception as exc:  # the collection itself already succeeded or recorded its status
        logger.warning("TAIFEX daily freshness check failed: %s", exc)
        result["freshness"] = {"error": str(exc)[:300]}
    return result
