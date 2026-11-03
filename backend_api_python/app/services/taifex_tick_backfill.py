"""Scheduled TAIFEX tick backfill: fill gaps in the daily Fubon 1m accumulation.

The Fubon collector (``tw_kline_collector``) misses minutes whenever the sidecar
is disconnected. A Celery Beat tick (``quantdinger.tasks.taifex_tick_backfill``)
stores every trade of each recent trading day before today as ``source=taifex``
1m bars. The upsert never replaces ``source=fubon`` bars, so only the missing
minutes are added. Daily bars are collected separately from the OpenAPI
``DailyMarketReportFut`` (``app.services.taifex_daily_openapi``); older history
is a manual import (``scripts/import_taifex_history.py``).

Sources, chosen per trading day
-------------------------------

* **OpenAPI** ``GET /v1/TimeAndSalesData``: no parameters, only the latest
  trading day. :meth:`TaifexOpenApiClient.probe` reads the first row to learn
  which trading day it serves; that day is streamed row by row, keeping only
  the ticks of the configured products (memory does not grow with the 227 MB
  response). Fields: ``Date`` (calendar date of the trade), ``ProductCode``,
  ``ContractMonth(Week)``, ``TimeOfTrades`` (``HHMMSS``), ``TradePrice`` and
  ``Volume(Buy+Sell)`` (both sides, so contracts = volume / 2). Spreads (a
  ``/`` in the month) and weekly contracts are skipped.
* **Web ZIP** ``Daily_YYYY_MM_DD.zip`` (:mod:`app.services.taifex_history`):
  every other day still on the TAIFEX website, which keeps about 30 days.
  ``Daily_D`` holds the night session opened on the previous trading day plus
  day D's regular session, i.e. exactly trading day D (observed 2026-11-03:
  ``Daily_2026_10_27`` starts at 10/22 15:00 because 10/23 and 10/26 were
  holidays), the same range as the OpenAPI response for D.
* Older than :data:`WEB_ZIP_RETENTION_DAYS` and still not ``success`` ->
  ``missed`` with a warning; ``scripts/import_taifex_history.py --ticks`` can
  import a tick file kept elsewhere.

Both sources share ``Date + time`` as Asia/Taipei wall time, the session rule
``bar_session`` and ``ticks_to_minute_bars``, so they yield identical bars.

Each trading day's outcome is recorded in ``qd_tw_kline_collect_status`` under
``(TWFutures, TAIFEX_TICKS, all, day)``: ``success`` and ``missed`` days are
skipped, ``failed`` days (not published yet, network error) are retried by the
next tick while they are within the lookback window. The status text
(``last_error``) starts with ``source=openapi`` or ``source=web_zip`` so the
record shows which source a day used.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from app.data_sources.tw_futures import trading_day_of
from app.data_sources.tw_futures_contracts import TAIPEI_TZ, TradingDayCheck, xtai_is_trading_day
from app.services.taifex_history import (
    DEFAULT_PRODUCTS,
    ImportReport,
    TaifexHistoryImporter,
    Tick,
    contract_symbol,
    parse_products,
)
from app.services.taifex_openapi import TaifexOpenApiClient, parse_number, parse_row_date
from app.services.tw_kline_store import MARKET_FUTURES, STATUS_FAILED, STATUS_MISSED, STATUS_SUCCESS
from app.utils.logger import get_logger

logger = get_logger(__name__)

STATUS_SYMBOL = "TAIFEX_TICKS"
STATUS_SESSION = "all"
# Trading days (before today) checked by every tick.
LOOKBACK_TRADING_DAYS = 5
OPENAPI_ENDPOINT = "TimeAndSalesData"
# Calendar days the TAIFEX website keeps ``Daily_YYYY_MM_DD.zip``.
WEB_ZIP_RETENTION_DAYS = 30

SOURCE_OPENAPI = "openapi"
SOURCE_WEB_ZIP = "web_zip"

_MANUAL_HINT = "import a kept tick file with scripts/import_taifex_history.py --ticks"


def _enabled() -> bool:
    return os.getenv("TAIFEX_TICK_BACKFILL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def configured_products(raw: Optional[str] = None) -> List[str]:
    text = os.getenv("TAIFEX_TICK_BACKFILL_PRODUCTS", ",".join(DEFAULT_PRODUCTS)) if raw is None else raw
    return parse_products(text)


def pending_days(today: date, is_trading_day: TradingDayCheck, count: int = LOOKBACK_TRADING_DAYS) -> List[date]:
    """The ``count`` most recent trading days strictly before ``today``, oldest first."""
    out: List[date] = []
    cursor = today - timedelta(days=1)
    for _ in range(count * 4 + 14):
        if len(out) >= count:
            break
        if is_trading_day(cursor):
            out.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(out)


def web_zip_available(day: date, today: date) -> bool:
    """Whether the TAIFEX website still keeps ``Daily_YYYY_MM_DD.zip`` of ``day``."""
    return (today - day).days <= WEB_ZIP_RETENTION_DAYS


# --------------------------------------------------------------------------- OpenAPI rows
def _row_timestamp(row: Dict[str, Any]) -> Optional[int]:
    """``Date`` + ``TimeOfTrades`` (Asia/Taipei wall time) -> unix seconds."""
    day = parse_row_date(row.get("Date"))
    digits = re.sub(r"\D", "", str(row.get("TimeOfTrades") or "").split(".", 1)[0])
    clock = digits.zfill(6)
    if day is None or not digits or len(clock) != 6:
        return None
    try:
        moment = datetime.combine(day, dtime(int(clock[:2]), int(clock[2:4]), int(clock[4:6])), tzinfo=TAIPEI_TZ)
    except ValueError:
        return None
    return int(moment.timestamp())


def parse_openapi_tick(row: Dict[str, Any], wanted: Optional[set] = None) -> Optional[Tick]:
    """One ``TimeAndSalesData`` object -> :class:`Tick`, or None when skipped.

    The product filter runs first so rows of other products cost one dict lookup.
    """
    code = str(row.get("ProductCode") or "").strip().upper()
    if wanted and code not in wanted:
        return None
    symbol = contract_symbol(code, str(row.get("ContractMonth(Week)") or ""))
    if symbol is None:
        return None
    price = parse_number(row.get("TradePrice"))
    volume = parse_number(row.get("Volume(Buy+Sell)"))
    ts = _row_timestamp(row)
    if price is None or volume is None or ts is None:
        return None
    return Tick(symbol, ts, price, volume / 2.0)


def iter_openapi_ticks(rows: Iterable[Dict[str, Any]], products: Optional[Iterable[str]] = None) -> Iterator[Tick]:
    """Stream the ticks of ``products`` (TAIFEX codes) out of ``TimeAndSalesData`` rows."""
    wanted = set(parse_products(list(products))) if products is not None else set()
    for row in rows:
        tick = parse_openapi_tick(row, wanted)
        if tick is not None:
            yield tick


def served_trading_day(first_row: Optional[Dict[str, Any]], is_trading_day: TradingDayCheck) -> Optional[date]:
    """Trading day of the response whose first row is ``first_row``.

    The first row may be the night session opened on the previous trading day
    (``Date=20261029 150000`` belongs to trading day 2026-10-30).
    """
    if not first_row:
        return None
    ts = _row_timestamp(first_row)
    if ts is None:
        return None
    return trading_day_of(ts, is_trading_day) or parse_row_date(first_row.get("Date"))


# --------------------------------------------------------------------------- backfill
def _detail(source: str, message: str = "") -> str:
    return f"source={source}" + (f": {message}" if message else "")


def _probe(client: Any, check: TradingDayCheck) -> Tuple[Optional[date], str]:
    """``(trading day the OpenAPI serves, error)``; the day is None when unknown."""
    try:
        probe = client.probe(OPENAPI_ENDPOINT)
    except Exception as exc:  # the web ZIP still covers the day
        logger.warning("TAIFEX %s probe failed; using the web ZIP files: %s", OPENAPI_ENDPOINT, exc)
        return None, f"{OPENAPI_ENDPOINT} probe failed: {exc}"
    served = served_trading_day(probe.first_row, check)
    if served is None:
        return None, f"{OPENAPI_ENDPOINT} returned no rows"
    return served, ""


def import_openapi_day(day: date, *, client: Any, importer: Any, products: List[str]) -> ImportReport:
    """Stream ``TimeAndSalesData`` and store its 1m bars; raises unless it holds trading day ``day``."""
    response = client.fetch(OPENAPI_ENDPOINT)
    try:
        ticks = list(iter_openapi_ticks(response, products))
    finally:
        response.close()
    if response.data_date != day:
        served = response.data_date.isoformat() if response.data_date else "nothing"
        raise RuntimeError(f"{OPENAPI_ENDPOINT} now serves {served}, not {day.isoformat()}")
    if not ticks:
        raise RuntimeError(f"{OPENAPI_ENDPOINT} has no trades for {','.join(products)}")
    return importer.import_ticks(ticks)


def run_backfill(
    now: Optional[datetime] = None,
    *,
    importer: Any = None,
    repository: Any = None,
    client: Any = None,
    products: Optional[List[str]] = None,
    is_trading_day: Optional[TradingDayCheck] = None,
    lookback: int = LOOKBACK_TRADING_DAYS,
) -> Dict[str, Any]:
    """One tick: import every pending recent trading day from the OpenAPI or the web ZIP."""
    check: Callable[[date], bool] = is_trading_day or xtai_is_trading_day
    if repository is None:
        from app.services.tw_kline_store import TWKlineRepository

        repository = TWKlineRepository()
    importer = importer or TaifexHistoryImporter(repository=repository, is_trading_day=check)
    client = client or TaifexOpenApiClient()
    wanted = products if products is not None else configured_products()

    today = (now or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ).date()
    days = pending_days(today, check, lookback)
    if not days:
        return {"skipped": "no trading day to backfill"}
    statuses = repository.load_statuses(days[0])
    pending = [
        day for day in days
        if statuses.get((MARKET_FUTURES, STATUS_SYMBOL, STATUS_SESSION, day)) not in (STATUS_SUCCESS, STATUS_MISSED)
    ]
    result: Dict[str, Any] = {"served": None, "imported": [], "failed": [], "missed": []}
    if not pending:
        result["skipped"] = "every recent trading day already backfilled"
        return result

    served, probe_error = _probe(client, check)
    result["served"] = served.isoformat() if served else None

    def record(day: date, status: str, detail: str, bars: int = 0, attempted: bool = True) -> None:
        repository.record_status(
            market=MARKET_FUTURES, symbol=STATUS_SYMBOL, session=STATUS_SESSION,
            trade_date=day, status=status, bar_count=bars, error=detail, attempted=attempted,
        )

    for day in pending:
        if served is not None and day == served:
            source = SOURCE_OPENAPI
        elif web_zip_available(day, today):
            source = SOURCE_WEB_ZIP
        else:
            reason = f"older than the {WEB_ZIP_RETENTION_DAYS}-day web ZIP retention; {_MANUAL_HINT}"
            logger.warning("TAIFEX tick backfill missed %s: %s", day, reason)
            record(day, STATUS_MISSED, reason, attempted=False)
            result["missed"].append({"trade_date": day.isoformat(), "error": reason})
            continue
        try:
            if source == SOURCE_OPENAPI:
                report = import_openapi_day(day, client=client, importer=importer, products=wanted)
            else:
                report = importer.import_tick_day(day, products=wanted)
        except Exception as exc:  # retried by the next tick
            context = f"; {probe_error}" if probe_error and source == SOURCE_WEB_ZIP else ""
            detail = _detail(source, f"{exc}{context}")
            logger.warning("TAIFEX tick backfill failed for %s (retried next run): %s", day, detail)
            record(day, STATUS_FAILED, detail)
            result["failed"].append({"trade_date": day.isoformat(), "source": source, "error": detail[:300]})
            continue
        logger.info("TAIFEX tick backfill stored %s from %s: %d bars", day, source, report.total)
        record(day, STATUS_SUCCESS, _detail(source), bars=report.total)
        result["imported"].append({"trade_date": day.isoformat(), "source": source, "bars": report.total})
    return result


def run_taifex_tick_backfill(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Celery Beat entry point; only runs where the Fubon accumulation runs."""
    if not _enabled():
        return {"skipped": "TAIFEX_TICK_BACKFILL_ENABLED is off"}
    from app.config.data_sources import FubonSidecarConfig

    if not FubonSidecarConfig.CONFIGURED:
        return {"skipped": "Fubon sidecar is not configured"}
    return run_backfill(now)
