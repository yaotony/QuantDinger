"""Parameter sweep and walk-forward optimization jobs (class B).

Agents optimize a saved Strategy API V2 source without hand-rolling one
backtest per parameter set. Each study runs as an async job of kind
``parameter_optimization``; the agent polls ``/jobs/{id}``. Results always
separate the in-sample (selection) metrics from the out-of-sample
(validation only) metrics, and report how the chosen parameters performed
out of sample. The best parameters' in-sample and out-of-sample runs are
saved to backtest history (``result.history``).

Every study is bounded by a combination cap, a per-tenant concurrency cap and
a per-tenant daily cap (``AGENT_OPTIMIZATION_*`` environment variables).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.backtest_execution import (
    default_commission_if_missing,
    default_slippage_if_missing,
)
from app.services.backtest_jobs import parse_backtest_date
from app.services.billing_service import BillingError
from app.services.script_source import get_script_source_service
from app.services.strategy_evolution.agent_optimization import (
    MODE_SWEEP,
    MODE_WALK_FORWARD,
    MODES,
    OBJECTIVES,
    OptimizationRequestError,
    optimization_limits,
    prepare_study,
    run_study,
)
from app.services.strategy_evolution.parameter_space import build_parameter_space
from app.services.strategy_v2 import StrategyV2BacktestService
from app.utils.agent_auth import SCOPE_B, SCOPE_R, agent_required, current_token, current_user_id, with_idempotency
from app.utils.agent_jobs import (
    _job_receipt,
    concurrent_job_limits,
    count_active_jobs,
    count_jobs_created_since,
    submit_job,
)
from app.utils.logger import get_logger
from flask import request

from . import agent_v1_bp
from . import backtests as backtest_routes
from ._helpers import envelope, error, get_json_or_400

logger = get_logger(__name__)

OPTIMIZATION_JOB_KIND = "parameter_optimization"
MODE_KEY = "__optimization_mode"
_COMMON_FIELDS = frozenset({
    "sourceId", "sourceVersion", "startDate", "endDate",
    "initialCapital", "commission", "slippage", "leverageEnabled", "leverage",
    "parameterSpace", "maxCombinations", "objective", "seed", "embargoBars",
})
REQUEST_FIELDS = {
    MODE_SWEEP: _COMMON_FIELDS | {"outOfSampleRatio"},
    MODE_WALK_FORWARD: _COMMON_FIELDS | {"folds", "trainRatio", "holdoutRatio"},
}

_backtest_service: StrategyV2BacktestService | None = None


def _get_backtest_service() -> StrategyV2BacktestService:
    global _backtest_service
    if _backtest_service is None:
        _backtest_service = StrategyV2BacktestService()
    return _backtest_service


def _make_evaluator(**kwargs: Any):
    from app.services.strategy_evolution.evaluator import PreparedEvolutionEvaluator

    return PreparedEvolutionEvaluator(backtest_service=_get_backtest_service(), **kwargs)


def _validate_costs(body: dict) -> Any:
    for field in ("commission", "slippage"):
        value = body.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            return error(400, f"{field} must be a number between 0 and 1")
    leverage = body.get("leverage")
    if leverage is not None and (
        isinstance(leverage, bool) or not isinstance(leverage, (int, float)) or float(leverage) < 1
    ):
        return error(400, "leverage must be a number >= 1")
    return None


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _limit_error(message: str, error_type: str, *, retriable: bool, **details: Any):
    return error(
        429,
        message,
        details={"error_type": error_type, **details},
        retriable=retriable,
        http=429,
    )


def _check_job_limits(token_id: int):
    """Return a 429 response when a concurrency or daily cap is reached."""
    user_id = current_user_id()
    limits = optimization_limits()
    tenant_cap, token_cap = concurrent_job_limits()
    if count_active_jobs(user_id=user_id) >= tenant_cap:
        return _limit_error("Tenant concurrent job limit reached", "TENANT_CONCURRENCY_LIMIT",
                            retriable=True, limit=tenant_cap)
    if count_active_jobs(user_id=user_id, agent_token_id=token_id) >= token_cap:
        return _limit_error("Token concurrent job limit reached", "TOKEN_CONCURRENCY_LIMIT",
                            retriable=True, limit=token_cap)
    active = count_active_jobs(user_id=user_id, kind=OPTIMIZATION_JOB_KIND)
    if active >= limits.max_concurrent_per_tenant:
        return _limit_error(
            "Optimization concurrent job limit reached",
            "OPTIMIZATION_CONCURRENCY_LIMIT",
            retriable=True,
            limit=limits.max_concurrent_per_tenant,
            active=active,
        )
    # Job rows store naive UTC created_at timestamps.
    day_start = _utc_day_start(datetime.now(timezone.utc).replace(tzinfo=None))
    used = count_jobs_created_since(user_id=user_id, kind=OPTIMIZATION_JOB_KIND, since=day_start)
    if used >= limits.max_daily_per_tenant:
        return _limit_error(
            "Optimization daily limit reached",
            "OPTIMIZATION_DAILY_LIMIT",
            retriable=False,
            limit=limits.max_daily_per_tenant,
            used=used,
            resetAt=(day_start + timedelta(days=1)).isoformat() + "Z",
        )
    return None


def _resolve_owned_source(body: dict) -> tuple[str, dict | None, dict | None, Any]:
    """Resolve the tenant-owned source version and the source row it belongs to."""
    if body.get("sourceId") is None:
        return "", None, None, error(400, "sourceId is required")
    code, ref, err = backtest_routes._resolve_source(body)
    if err:
        return "", None, None, err
    source = get_script_source_service().get_source(int(ref["sourceId"]), user_id=current_user_id())
    if not source:
        return "", None, None, error(404, "Strategy source not found", http=404)
    return code, ref, source, None


def _submit(mode: str):
    body, err = get_json_or_400()
    if err:
        return err
    unsupported = sorted(set(body) - REQUEST_FIELDS[mode])
    if unsupported:
        return error(400, f"Unsupported optimization fields: {', '.join(unsupported)}")
    err = _validate_costs(body)
    if err:
        return err
    code, source_ref, source, err = _resolve_owned_source(body)
    if err:
        return err
    # Same compile, market/instrument allowlist and range checks as /backtest/run.
    _program, err = backtest_routes._validate_code(
        {key: body[key] for key in ("startDate", "endDate", "initialCapital") if key in body},
        code,
    )
    if err:
        return err
    limits = optimization_limits()
    try:
        study = prepare_study(source=source, code=code, mode=mode, payload=body, limits=limits)
    except OptimizationRequestError as exc:
        return error(400, str(exc), details=exc.details)

    with with_idempotency(OPTIMIZATION_JOB_KIND) as existing:
        if existing:
            return envelope(_job_receipt(existing, duplicate=True), message="idempotent replay")

    token_id = int(current_token().get("id") or 0)
    limit_error = _check_job_limits(token_id)
    if limit_error:
        return limit_error

    payload = dict(body)
    payload["__user_id"] = current_user_id()
    payload[MODE_KEY] = mode
    payload[backtest_routes.SOURCE_REF_KEY] = source_ref
    try:
        job = submit_job(
            user_id=current_user_id(),
            agent_token_id=token_id,
            kind=OPTIMIZATION_JOB_KIND,
            request_payload=payload,
            runner=_run_optimization,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except BillingError as exc:
        return error(exc.status, exc.code, details={"error_type": exc.code, **exc.details},
                     retriable=exc.status >= 500, http=exc.status)
    except Exception:
        logger.exception("Agent optimization submission failed")
        return error(503, "AGENT_JOB_UNAVAILABLE", retriable=True, http=503)
    if job["status"] == "failed":
        return error(503, "AGENT_JOB_DISPATCH_FAILED", details=job, http=503)
    return envelope({
        **job,
        "mode": mode,
        "combinations": study.combinations,
        "searchSpaceSize": study.search_space_size,
        "strategySource": backtest_routes._public_source_ref(source_ref),
        "limits": limits.public(),
    }, message="queued", status=202)


@agent_v1_bp.route("/optimization/sweep", methods=["POST"])
@agent_required(SCOPE_B)
def submit_parameter_sweep():
    """Queue a parameter sweep: select on in-sample, validate once out of sample."""
    return _submit(MODE_SWEEP)


@agent_v1_bp.route("/optimization/walk-forward", methods=["POST"])
@agent_required(SCOPE_B)
def submit_walk_forward():
    """Queue a walk-forward study: per-fold in-sample selection, out-of-sample validation."""
    return _submit(MODE_WALK_FORWARD)


@agent_v1_bp.route("/optimization/parameter-space", methods=["GET"])
@agent_required(SCOPE_R)
def get_optimization_parameter_space():
    """Return a source version's declared parameter space and the optimization limits."""
    query: dict[str, Any] = {}
    for name in ("sourceId", "sourceVersion"):
        raw = str(request.args.get(name) or "").strip()
        if not raw:
            continue
        try:
            query[name] = int(raw)
        except ValueError:
            return error(400, f"{name} must be a positive integer")
    code, source_ref, source, err = _resolve_owned_source(query)
    if err:
        return err
    space = build_parameter_space({**source, "code": code})
    return envelope({
        **space,
        "strategySource": backtest_routes._public_source_ref(source_ref),
        "modes": list(MODES),
        "objectives": list(OBJECTIVES),
        "limits": optimization_limits().public(),
    })


def _load_pinned_source(payload: dict, ref: dict) -> tuple[str, dict]:
    user_id = int(payload.get("__user_id") or 0)
    source_id = int(ref.get("sourceId") or 0)
    version_id = int(ref.get("versionId") or 0)
    sources = get_script_source_service()
    version = sources.get_version(version_id, user_id) if user_id and source_id and version_id else None
    if not version or int(version.get("source_id") or 0) != source_id:
        raise ValueError("strategyV2.sourceVersionNotFound")
    source = sources.get_source(source_id, user_id=user_id)
    if not source:
        raise ValueError("strategyEvolution.sourceNotFound")
    return str(version.get("code") or ""), source


def _run_optimization(payload: dict, on_progress=None) -> dict:
    ref = payload.get(backtest_routes.SOURCE_REF_KEY)
    if not isinstance(ref, dict):
        raise ValueError("strategyV2.sourceVersionNotFound")
    mode = str(payload.get(MODE_KEY) or "")
    code, source = _load_pinned_source(payload, ref)
    study = prepare_study(
        source=source,
        code=code,
        mode=mode,
        payload=payload,
        limits=optimization_limits(),
    )
    start_date = parse_backtest_date(payload.get("startDate"))
    end_date = parse_backtest_date(payload.get("endDate"))
    if not start_date or not end_date:
        raise ValueError("startDate and endDate are required (YYYY-MM-DD)")
    end_date = end_date.replace(hour=23, minute=59, second=59)
    leverage_enabled = bool(payload.get("leverageEnabled"))
    evaluator = _make_evaluator(
        user_id=int(payload["__user_id"]),
        code=study.code,
        start_date=start_date,
        end_date=end_date,
        initial_capital=float(payload.get("initialCapital") or 10_000),
        leverage_enabled=leverage_enabled,
        leverage=float(payload.get("leverage") or 1) if leverage_enabled else 1.0,
        source_id=int(ref["sourceId"]),
        strategy_name=str(ref.get("name") or ""),
    )
    result = run_study(
        study,
        evaluator=evaluator,
        start_date=start_date,
        end_date=end_date,
        commission=default_commission_if_missing(payload.get("commission")),
        slippage=default_slippage_if_missing(payload.get("slippage")),
        on_progress=on_progress,
    )
    return {**result, "strategySource": backtest_routes._public_source_ref(ref)}
