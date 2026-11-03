"""Re-attribute stored TAIFEX daily bars after an unscheduled market closure.

Typhoon days and other unscheduled closures are not in the ``exchange_calendars``
XTAI calendar. Until such a day is listed in
``BUILTIN_TW_MARKET_CLOSED_DAYS`` or ``TW_MARKET_CLOSED_DAYS``, the system
treats it as a trading day, so:

* the night session opened on the previous trading day is stored as a ``1D``
  bar labelled on the closed day (``afterhours`` row at Asia/Taipei midnight of
  the closed day) instead of on the real next trading day, which in turn lacks
  its night session;
* the daily collectors record ``failed`` statuses for the closed day, because
  TAIFEX publishes nothing for it.

Once the day is listed, new data is attributed correctly, but rows already
stored stay wrong. :func:`build_plan` finds them and :func:`apply_plan`

1. deletes the ``TWFutures`` ``1D`` rows labelled on a closed day,
2. moves ``failed`` collection statuses of the closed day to ``missed`` with a
   note (the day has nothing to collect), and
3. re-imports the TAIFEX website daily report from the previous trading day to
   the next trading day (an idempotent upsert), which now labels the night
   session on the correct trading day.

This is a one-off operator command (``scripts/repair_tw_market_closed_days.py``),
never an upgrade-time migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.data_sources.tw_futures_contracts import (
    BUILTIN_TW_MARKET_CLOSED_DAYS,
    TAIPEI_TZ,
    TradingDayCheck,
    next_trading_day,
    xtai_is_trading_day,
)
from app.services.taifex_history import DEFAULT_PRODUCTS, TIMEFRAME_DAILY, parse_products, to_taifex_product
from app.services.tw_kline_store import MARKET_FUTURES, STATUS_FAILED, STATUS_MISSED

CLOSED_DAY_NOTE = "market closed all day ({reason}); nothing to collect"


class ClosedDayError(ValueError):
    """A day to repair that the trading calendar still treats as a trading day."""


def _midnight_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ).timestamp())


def previous_trading_day(day: date, is_trading_day: Optional[TradingDayCheck] = None) -> date:
    """The last trading day strictly before ``day``."""
    check = is_trading_day or xtai_is_trading_day
    candidate = day - timedelta(days=1)
    for _ in range(31):
        if check(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return day - timedelta(days=1)


def reimport_ranges(days: Iterable[date], is_trading_day: Optional[TradingDayCheck] = None) -> List[Tuple[date, date]]:
    """``[previous trading day, next trading day]`` around each closed day, merged when they overlap."""
    check = is_trading_day or xtai_is_trading_day
    spans = sorted(
        (previous_trading_day(day, check), next_trading_day(day, check)) for day in set(days)
    )
    merged: List[Tuple[date, date]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def closed_day_note(day: date) -> str:
    reason = BUILTIN_TW_MARKET_CLOSED_DAYS.get(day, "listed in TW_MARKET_CLOSED_DAYS")
    return CLOSED_DAY_NOTE.format(reason=reason)


@dataclass
class RepairPlan:
    closed_days: List[date]
    bad_bars: List[Dict[str, Any]] = field(default_factory=list)
    failed_statuses: List[Dict[str, Any]] = field(default_factory=list)
    reimport: List[Tuple[date, date]] = field(default_factory=list)
    products: List[str] = field(default_factory=list)

    def bar_keys(self) -> List[Tuple[str, str, str, str, int]]:
        return [(b["market"], b["symbol"], b["session"], b["timeframe"], int(b["time"])) for b in self.bad_bars]

    def status_keys(self) -> List[Tuple[str, str, str, date]]:
        return [(s["market"], s["symbol"], s["session"], s["trade_date"]) for s in self.failed_statuses]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "closed_days": [day.isoformat() for day in self.closed_days],
            "delete_daily_bars": [
                {
                    "market": bar["market"],
                    "symbol": bar["symbol"],
                    "session": bar["session"],
                    "timeframe": bar["timeframe"],
                    "trade_date": datetime.fromtimestamp(int(bar["time"]), tz=TAIPEI_TZ).date().isoformat(),
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "source": bar["source"],
                }
                for bar in self.bad_bars
            ],
            "mark_statuses_missed": [
                {
                    "market": row["market"],
                    "symbol": row["symbol"],
                    "session": row["session"],
                    "trade_date": row["trade_date"].isoformat(),
                    "attempts": row.get("attempts", 0),
                    "last_error": row.get("last_error", ""),
                    "note": closed_day_note(row["trade_date"]),
                }
                for row in self.failed_statuses
            ],
            "reimport_daily": [
                {"from": start.isoformat(), "to": end.isoformat(), "products": list(self.products)}
                for start, end in self.reimport
            ],
        }


def _products_of(bars: Sequence[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for bar in bars:
        product = to_taifex_product(str(bar.get("symbol") or "")[:3])
        if product and product not in out:
            out.append(product)
    return out


def build_plan(
    days: Iterable[date],
    *,
    repository: Any,
    products: Optional[Sequence[str]] = None,
    is_trading_day: Optional[TradingDayCheck] = None,
) -> RepairPlan:
    """Read-only: the rows to delete / mark and the date ranges to re-import.

    Raises :class:`ClosedDayError` when a day is still a trading day for the
    calendar: re-importing would label the night session on it again.
    """
    check = is_trading_day or xtai_is_trading_day
    closed = sorted(set(days))
    still_open = [day for day in closed if check(day)]
    if still_open:
        listed = ",".join(day.isoformat() for day in still_open)
        raise ClosedDayError(
            f"{listed} is still a trading day; add it to TW_MARKET_CLOSED_DAYS (or the built-in list) first"
        )
    plan = RepairPlan(closed_days=closed)
    if not closed:
        return plan
    plan.bad_bars = list(
        repository.find_bars_at(
            market=MARKET_FUTURES, timeframe=TIMEFRAME_DAILY, bar_times=[_midnight_ts(day) for day in closed],
        )
    )
    plan.failed_statuses = list(repository.find_statuses(trade_dates=closed, status=STATUS_FAILED))
    plan.reimport = reimport_ranges(closed, check)
    if products:
        plan.products = parse_products(list(products))
    else:
        plan.products = _products_of(plan.bad_bars) or list(DEFAULT_PRODUCTS)
    return plan


def apply_plan(plan: RepairPlan, *, repository: Any, importer: Any) -> Dict[str, Any]:
    """Delete, mark, then re-import; re-import failures are reported (a re-run retries them)."""
    deleted = repository.delete_bars(plan.bar_keys())
    marked = 0
    for row in plan.failed_statuses:
        marked += repository.mark_statuses(
            [(row["market"], row["symbol"], row["session"], row["trade_date"])],
            from_status=STATUS_FAILED,
            to_status=STATUS_MISSED,
            note=closed_day_note(row["trade_date"]),
        )
    imports: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for start, end in plan.reimport:
        report = importer.import_daily(start, end, plan.products)
        imports.append({"from": start.isoformat(), "to": end.isoformat(), **report.as_dict()})
        failed.extend(report.failed)
    return {"deleted_daily_bars": deleted, "marked_missed": marked, "reimport_daily": imports, "failed": failed}
