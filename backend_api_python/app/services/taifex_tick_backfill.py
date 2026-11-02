"""Scheduled TAIFEX tick backfill: fill gaps in the daily Fubon 1m accumulation.

The Fubon collector (``tw_kline_collector``) misses minutes whenever the sidecar
is disconnected. TAIFEX publishes every trading day's trades
(``Daily_YYYY_MM_DD``, kept for 30 days), so a Celery Beat tick
(``quantdinger.tasks.taifex_tick_backfill``) downloads the file of each recent
trading day before today and stores it as ``source=taifex`` 1m bars. The upsert
never replaces ``source=fubon`` bars, so only the missing minutes are added.

Each trading day's outcome is recorded in ``qd_tw_kline_collect_status`` under
``(TWFutures, TAIFEX_TICKS, all, day)``: ``success`` days are skipped, ``failed``
days (file not published yet, network error) are retried by the next tick while
they are within the lookback window.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.data_sources.tw_futures_contracts import TAIPEI_TZ, TradingDayCheck, xtai_is_trading_day
from app.services.taifex_history import DEFAULT_PRODUCTS, TaifexHistoryImporter, parse_products
from app.services.tw_kline_store import MARKET_FUTURES, STATUS_FAILED, STATUS_SUCCESS
from app.utils.logger import get_logger

logger = get_logger(__name__)

STATUS_SYMBOL = "TAIFEX_TICKS"
STATUS_SESSION = "all"
# Trading days (before today) checked by every tick.
LOOKBACK_TRADING_DAYS = 5


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


def run_backfill(
    now: Optional[datetime] = None,
    *,
    importer: Any = None,
    repository: Any = None,
    products: Optional[List[str]] = None,
    is_trading_day: Optional[TradingDayCheck] = None,
    lookback: int = LOOKBACK_TRADING_DAYS,
) -> Dict[str, Any]:
    """One tick: import every pending recent trading day's tick file."""
    check: Callable[[date], bool] = is_trading_day or xtai_is_trading_day
    if repository is None:
        from app.services.tw_kline_store import TWKlineRepository

        repository = TWKlineRepository()
    importer = importer or TaifexHistoryImporter(repository=repository, is_trading_day=check)
    wanted = products if products is not None else configured_products()

    today = (now or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ).date()
    days = pending_days(today, check, lookback)
    if not days:
        return {"skipped": "no trading day to backfill"}
    statuses = repository.load_statuses(days[0])
    result: Dict[str, Any] = {"imported": [], "failed": []}
    for day in days:
        if statuses.get((MARKET_FUTURES, STATUS_SYMBOL, STATUS_SESSION, day)) == STATUS_SUCCESS:
            continue
        try:
            report = importer.import_tick_day(day, products=wanted)
        except Exception as exc:  # retried by the next tick
            logger.warning("TAIFEX tick backfill failed for %s (retried next run): %s", day, exc)
            repository.record_status(
                market=MARKET_FUTURES, symbol=STATUS_SYMBOL, session=STATUS_SESSION,
                trade_date=day, status=STATUS_FAILED, error=str(exc),
            )
            result["failed"].append({"trade_date": day.isoformat(), "error": str(exc)[:300]})
            continue
        repository.record_status(
            market=MARKET_FUTURES, symbol=STATUS_SYMBOL, session=STATUS_SESSION,
            trade_date=day, status=STATUS_SUCCESS, bar_count=report.total,
        )
        result["imported"].append({"trade_date": day.isoformat(), "bars": report.total})
    return result


def run_taifex_tick_backfill(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Celery Beat entry point; only runs where the Fubon accumulation runs."""
    if not _enabled():
        return {"skipped": "TAIFEX_TICK_BACKFILL_ENABLED is off"}
    from app.config.data_sources import FubonSidecarConfig

    if not FubonSidecarConfig.CONFIGURED:
        return {"skipped": "Fubon sidecar is not configured"}
    return run_backfill(now)
