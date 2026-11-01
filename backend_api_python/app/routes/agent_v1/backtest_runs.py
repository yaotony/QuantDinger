"""Backtest history endpoints (class R).

Agents submit backtests through ``POST /backtest/run`` and receive a job; the
finished job result carries ``runId``. These endpoints read the persisted
runs (the same records the Backtest Center history shows) so an agent can
compare iterations without scraping ``/jobs``. Every query is filtered by the
token owner's ``user_id``; another tenant's run id resolves to 404.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from app.services.strategy_v2 import StrategyBacktestRepository
from app.utils.agent_auth import SCOPE_R, agent_required, current_user_id
from app.utils.logger import get_logger
from flask import request

from . import agent_v1_bp
from ._helpers import clip_int, envelope, error

logger = get_logger(__name__)

LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 100

_repository: StrategyBacktestRepository | None = None


def get_backtest_run_repository() -> StrategyBacktestRepository:
    global _repository
    if _repository is None:
        _repository = StrategyBacktestRepository()
    return _repository


def _optional_positive_int(name: str) -> int | None:
    raw = str(request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_time(name: str, *, end_of_day: bool) -> datetime | None:
    """Parse ``YYYY-MM-DD`` or an ISO-8601 datetime query parameter.

    A bare date used as an upper bound covers the whole day. Timezone-aware
    values are compared on their wall-clock time because ``created_at`` is a
    naive server-local timestamp.
    """
    raw = str(request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
            return datetime.combine(day, time.max if end_of_day else time.min)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD or an ISO-8601 datetime") from None


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _metrics(row: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summary metrics; stored summary columns win, the full result fills gaps."""
    fallback = result if isinstance(result, dict) else {}

    def pick(column: str, field: str) -> Any:
        value = row.get(column)
        return value if value is not None else fallback.get(field)

    return {
        "totalReturn": _number(pick("total_return", "totalReturn")),
        "maxDrawdown": _number(pick("max_drawdown", "maxDrawdown")),
        "totalTrades": _integer(pick("total_trades", "totalTrades")),
        "winRate": _number(pick("win_rate", "winRate")),
        "totalFees": _number(pick("total_commission", "totalCommission")),
        "sharpeRatio": _number(pick("sharpe_ratio", "sharpeRatio")),
        "totalExecutions": _integer(pick("total_executions", "totalExecutions")),
        "benchmarkTotalReturn": _number(pick("benchmark_total_return", "benchmarkTotalReturn")),
    }


def _project_run(row: dict[str, Any], *, include_result: bool) -> dict[str, Any]:
    result = row.get("result") if include_result else None
    item = {
        "runId": row.get("id"),
        "strategyId": row.get("strategy_id"),
        "sourceId": row.get("source_id") or None,
        "strategyName": row.get("strategy_name") or "",
        "market": row.get("market") or "",
        "symbol": row.get("symbol") or "",
        "marketType": row.get("market_type") or "",
        "timeframe": row.get("timeframe") or "",
        "startDate": row.get("start_date") or "",
        "endDate": row.get("end_date") or "",
        "initialCapital": _number(row.get("initial_capital")),
        "commission": _number(row.get("commission")),
        "slippage": _number(row.get("slippage")),
        "leverage": _number(row.get("leverage")),
        "params": row.get("params") if isinstance(row.get("params"), dict) else {},
        "engineVersion": row.get("engine_version") or "",
        "codeHash": row.get("code_hash") or "",
        "status": row.get("status") or "",
        "resultStatus": row.get("result_status") or "unknown",
        "dataKind": row.get("data_kind") or "unknown",
        "createdAt": _iso(row.get("created_at")),
        "metrics": _metrics(row, result),
    }
    if include_result:
        item["manifest"] = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
        item["result"] = result if isinstance(result, dict) else {}
    return item


@agent_v1_bp.route("/backtest/runs", methods=["GET"])
@agent_required(SCOPE_R)
def list_backtest_runs():
    """List the tenant's persisted backtest runs, newest first, with summary metrics."""
    try:
        source_id = _optional_positive_int("sourceId")
        strategy_id = _optional_positive_int("strategyId")
        created_from = _optional_time("createdFrom", end_of_day=False)
        created_to = _optional_time("createdTo", end_of_day=True)
    except ValueError as exc:
        return error(400, str(exc))
    if created_from and created_to and created_to < created_from:
        return error(400, "createdTo must not be earlier than createdFrom")

    limit = clip_int(request.args.get("limit"), default=LIST_LIMIT_DEFAULT, lo=1, hi=LIST_LIMIT_MAX)
    offset = clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000)
    try:
        # Fetch one extra row to report hasMore without a COUNT(*) scan.
        rows = get_backtest_run_repository().list_runs(
            user_id=current_user_id(),
            limit=limit + 1,
            offset=offset,
            strategy_id=strategy_id,
            source_id=source_id,
            symbol=str(request.args.get("symbol") or "").strip(),
            market=str(request.args.get("market") or "").strip(),
            timeframe=str(request.args.get("timeframe") or "").strip(),
            created_from=created_from,
            created_to=created_to,
        ) or []
    except Exception:
        logger.exception("agent_v1 backtest run list failed")
        return error(500, "list_backtest_runs failed", retriable=True, http=500)

    has_more = len(rows) > limit
    items = [_project_run(row, include_result=False) for row in rows[:limit]]
    return envelope({
        "items": items,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "hasMore": has_more,
            "nextOffset": offset + limit if has_more else None,
        },
    })


@agent_v1_bp.route("/backtest/runs/<int:run_id>", methods=["GET"])
@agent_required(SCOPE_R)
def get_backtest_run(run_id: int):
    """Read one tenant-owned backtest run including its full stored result."""
    try:
        row = get_backtest_run_repository().get_run(user_id=current_user_id(), run_id=int(run_id))
    except Exception:
        logger.exception("agent_v1 backtest run lookup failed")
        return error(500, "get_backtest_run failed", retriable=True, http=500)
    if not row:
        return error(404, "Backtest run not found", http=404)
    return envelope(_project_run(row, include_result=True))
