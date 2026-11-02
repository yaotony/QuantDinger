"""Shared asynchronous runner for Strategy API V2 backtests."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.services.backtest_execution import (
    default_commission_if_missing,
    default_slippage_if_missing,
)
from app.services.strategy_v2 import StrategyV2BacktestService
from app.data_sources.tw_futures import normalize_session as normalize_tw_futures_session
from app.services.strategy_v2.funding import normalize_funding_mode


_backtest = StrategyV2BacktestService()


def parse_backtest_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {text}")


def run_backtest_job(
    payload: dict[str, Any],
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    *,
    service: StrategyV2BacktestService | None = None,
) -> dict[str, Any]:
    """Run a persisted backtest job for either the human UI or Agent API."""
    code = str(payload.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")

    start_date = parse_backtest_date(payload.get("startDate"))
    end_date = parse_backtest_date(payload.get("endDate"))
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required (YYYY-MM-DD)")

    params = payload.get("params") or {}
    leverage_enabled = bool(payload.get("leverageEnabled"))
    emitted_progress = False

    def report_progress(event: dict[str, Any]) -> None:
        nonlocal emitted_progress
        emitted_progress = True
        if on_progress:
            on_progress(event)

    run_id, result = (service or _backtest).run(
        user_id=int(payload.get("__user_id") or 1),
        code=code,
        start_date=start_date,
        end_date=end_date.replace(hour=23, minute=59, second=59),
        initial_capital=float(payload.get("initialCapital") or 10_000),
        leverage_enabled=leverage_enabled,
        leverage=float(payload.get("leverage") or 1) if leverage_enabled else 1.0,
        commission=default_commission_if_missing(payload.get("commission")),
        slippage=default_slippage_if_missing(payload.get("slippage")),
        params=params if isinstance(params, dict) else {},
        strategy_id=_positive_int(payload.get("__strategy_id")),
        source_id=_positive_int(payload.get("__source_id")),
        strategy_name=str(payload.get("__strategy_name") or ""),
        instrument_rules_snapshot_id=str(
            payload.get("instrumentRulesSnapshotId") or ""
        ).strip(),
        persist=bool(payload.get("persist", True)),
        on_progress=report_progress if on_progress else None,
        funding_mode=normalize_funding_mode(payload.get("fundingMode")),
        session=normalize_tw_futures_session(payload.get("session")),
    )
    if on_progress and not emitted_progress:
        on_progress({"phase": "finalizing", "percent": 95})
    return {**result, "runId": run_id}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
