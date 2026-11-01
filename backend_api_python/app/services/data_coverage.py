"""Market data coverage for backtest planning.

Answers, before a backtest is submitted, how far back a market/symbol/frequency
can be fetched, the backtest range limit (warmup included), and whether a
planned range is feasible. The range verdict is produced by the exact rule the
backtest precheck and job-time check use (``backtest_range_violation``), so a
coverage answer and a real submission cannot disagree for the same input.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.data_sources.factory import DataSourceFactory
from app.services.backtest_limits import backtest_range_policy, normalize_backtest_timeframe
from app.services.strategy_v2.frequencies import (
    SUPPORTED_SUBSCRIPTION_FREQUENCIES,
    normalize_frequency,
)
from app.services.strategy_v2.models import InstrumentSpec
from app.services.strategy_v2.service import (
    backtest_range_candidates,
    backtest_range_validation_market,
    backtest_range_violation,
    backtest_warmup_days,
)

EarliestLookup = Callable[..., Optional[int]]

_DATE_FORMAT = "%Y-%m-%d"


class DataCoverageRequestError(ValueError):
    """Invalid coverage input that callers should report as a 400."""


def coverage_frequency(timeframe: Any) -> str:
    """Normalize a timeframe the same way strategy manifests do."""
    frequency = normalize_frequency(timeframe)
    if frequency not in SUPPORTED_SUBSCRIPTION_FREQUENCIES:
        raise DataCoverageRequestError(
            "timeframe must be one of: " + ", ".join(SUPPORTED_SUBSCRIPTION_FREQUENCIES)
        )
    return frequency


def describe_data_coverage(
    *,
    market: str,
    symbol: str,
    timeframe: str,
    exchange_id: str = "",
    market_type: str = "",
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    warmup_bars: int = 0,
    earliest_lookup: Optional[EarliestLookup] = None,
) -> dict[str, Any]:
    """Return earliest data time, range limits, and an optional range verdict."""
    if (start_date is None) != (end_date is None):
        raise DataCoverageRequestError("start_date and end_date must be provided together")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise DataCoverageRequestError("end_date must not be earlier than start_date")
    warmup = int(warmup_bars or 0)
    if warmup < 0:
        raise DataCoverageRequestError("warmup_bars must be a non-negative integer")

    frequency = coverage_frequency(timeframe)
    instrument = InstrumentSpec(
        market=market,
        symbol=symbol,
        exchange_id=str(exchange_id or "").strip().lower(),
        market_type=str(market_type or "").strip().lower(),
    )
    candidates = backtest_range_candidates([instrument])
    candidate = candidates[0]
    validation_market = backtest_range_validation_market(candidate)
    policy = backtest_range_policy(validation_market, frequency)
    warmup_days = backtest_warmup_days(frequency, warmup, candidates)

    lookup = earliest_lookup or DataSourceFactory.get_earliest_kline_time
    earliest_ts = lookup(
        market=market,
        symbol=symbol,
        timeframe=normalize_backtest_timeframe(frequency),
        exchange_id=instrument.exchange_id or None,
        market_type=instrument.market_type or None,
    )
    earliest = (
        datetime.fromtimestamp(int(earliest_ts), tz=timezone.utc)
        if earliest_ts
        else None
    )

    result: dict[str, Any] = {
        "market": market,
        "symbol": symbol,
        "timeframe": frequency,
        "exchange_id": instrument.exchange_id,
        "market_type": instrument.market_type,
        "earliest_bar": {
            "status": "known" if earliest else "unknown",
            "time": earliest.isoformat().replace("+00:00", "Z") if earliest else None,
            "timestamp": int(earliest.timestamp()) if earliest else None,
        },
        "limits": {
            "validation_market": validation_market,
            "max_days": policy.max_days,
            "max_range": policy.label,
            "reason": policy.reason,
            "warmup_bars": warmup,
            "warmup_days": warmup_days,
            "max_selected_days": max(0, policy.max_days - warmup_days),
            "warmup_fits": warmup_days < policy.max_days,
        },
        "evaluation": None,
    }
    if start_date is not None and end_date is not None:
        result["evaluation"] = _evaluate_range(
            candidates=candidates,
            frequency=frequency,
            start_date=start_date,
            end_date=end_date,
            warmup_bars=warmup,
            warmup_days=warmup_days,
            earliest=earliest,
        )
    return result


def _evaluate_range(
    *,
    candidates: list[dict[str, Any]],
    frequency: str,
    start_date: datetime,
    end_date: datetime,
    warmup_bars: int,
    warmup_days: int,
    earliest: Optional[datetime],
) -> dict[str, Any]:
    def violation_for(start: datetime, end: datetime) -> Optional[dict[str, Any]]:
        return backtest_range_violation(
            candidates,
            timeframe=frequency,
            start_date=start,
            end_date=end,
            warmup_bars=warmup_bars,
        )

    violation = violation_for(start_date, end_date)
    fetch_start = start_date - timedelta(days=warmup_days)
    history_start = None
    history_status = "unknown"
    if earliest is not None:
        # Dates are compared as naive UTC days, like the backtest bounds. A
        # first candle opening mid-day leaves that day partially covered, so
        # the next day is the first fully covered one.
        earliest_day = datetime(earliest.year, earliest.month, earliest.day)
        if earliest.time() != datetime.min.time():
            earliest_day += timedelta(days=1)
        history_start = earliest_day + timedelta(days=warmup_days)
        history_status = "ok" if fetch_start >= earliest_day else "insufficient"

    feasible = violation is None and history_status != "insufficient"
    recommended_start, recommended_end = None, None
    if not feasible:
        recommended_start, recommended_end = _recommend(
            start_date=start_date,
            end_date=end_date,
            violation=violation,
            history_start=history_start if history_status == "insufficient" else None,
            violation_for=violation_for,
        )

    return {
        "start_date": start_date.strftime(_DATE_FORMAT),
        "end_date": end_date.strftime(_DATE_FORMAT),
        "feasible": feasible,
        "range_check": {
            "ok": violation is None,
            "violation": violation,
        },
        "history_check": {
            "status": history_status,
            "fetch_start": fetch_start.strftime(_DATE_FORMAT),
            "earliest_start": history_start.strftime(_DATE_FORMAT) if history_start else None,
        },
        "recommended_start": recommended_start,
        "recommended_end": recommended_end,
    }


def _recommend(
    *,
    start_date: datetime,
    end_date: datetime,
    violation: Optional[dict[str, Any]],
    history_start: Optional[datetime],
    violation_for: Callable[[datetime, datetime], Optional[dict[str, Any]]],
) -> tuple[Optional[str], Optional[str]]:
    """Return one feasible (start, end) pair that keeps the requested end date."""
    start = start_date
    if violation is not None:
        suggested = violation.get("recommended_start")
        if not violation.get("recommendation_available") or not suggested:
            return None, None
        start = max(start, datetime.strptime(str(suggested), _DATE_FORMAT))
    if history_start is not None:
        start = max(start, history_start)
    if start > end_date or violation_for(start, end_date) is not None:
        return None, None
    return start.strftime(_DATE_FORMAT), end_date.strftime(_DATE_FORMAT)
