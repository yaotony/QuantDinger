"""Bounded parameter sweeps and walk-forward studies for the Agent Gateway.

Both modes keep selection and validation apart:

* ``sweep`` splits the range chronologically. Every candidate is backtested on
  the in-sample window only; the best in-sample candidate is then run once on
  the out-of-sample window, which never influences the choice.
* ``walkForward`` builds anchored folds. For each fold the candidate with the
  best training (in-sample) objective is chosen and only then scored on that
  fold's validation (out-of-sample) window. The final parameters are chosen on
  the whole development window and validated once on a trailing holdout that
  no selection step has seen.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .constraints import ParameterConstraint, discover_parameter_constraints, first_rejection
from .engine import StrategyEvolutionFailure
from .models import SearchParameter
from .parameter_space import adapt_source_for_parameters, build_parameter_space
from .search import ParameterSampler
from .service import StrategyEvolutionService
from .statistics import metric_snapshot
from .walk_forward import WalkForwardFold, _as_datetime, _observation_times, build_walk_forward_plan

logger = logging.getLogger(__name__)

MODE_SWEEP = "sweep"
MODE_WALK_FORWARD = "walkForward"
MODES = (MODE_SWEEP, MODE_WALK_FORWARD)
OBJECTIVES = {"sharpeRatio": "sharpe", "totalReturn": "return"}
TOP_CANDIDATES = 10
MIN_IN_SAMPLE_BARS = 20
MIN_OUT_OF_SAMPLE_BARS = 10

Evaluator = Callable[[dict[str, Any], datetime, datetime, float, float], dict[str, Any]]
ProgressCallback = Callable[[dict[str, Any]], None]


class OptimizationRequestError(ValueError):
    """A request that violates the optimization contract (HTTP 400)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class OptimizationLimits:
    max_combinations: int
    max_concurrent_per_tenant: int
    max_daily_per_tenant: int

    def public(self) -> dict[str, int]:
        return {
            "maxCombinations": self.max_combinations,
            "maxConcurrentPerTenant": self.max_concurrent_per_tenant,
            "maxDailyPerTenant": self.max_daily_per_tenant,
        }


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _int_field(payload: dict[str, Any], name: str, default: int | None, low: int, high: int | None) -> int | None:
    value = payload.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f">= {low}"
        raise OptimizationRequestError(f"{name} must be an integer {bound}")
    return value


def _float_field(payload: dict[str, Any], name: str, default: float, low: float, high: float) -> float:
    value = payload.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise OptimizationRequestError(f"{name} must be a number between {low} and {high}")
    return float(value)


def optimization_limits() -> OptimizationLimits:
    """Operator-tunable caps for agent optimization jobs."""
    return OptimizationLimits(
        max_combinations=_env_int("AGENT_OPTIMIZATION_MAX_COMBINATIONS", 200),
        max_concurrent_per_tenant=_env_int("AGENT_OPTIMIZATION_MAX_CONCURRENT_PER_TENANT", 1),
        max_daily_per_tenant=_env_int("AGENT_OPTIMIZATION_MAX_DAILY_PER_TENANT", 20),
    )


@dataclass(frozen=True)
class OptimizationSettings:
    mode: str
    objective: str = "sharpeRatio"
    max_combinations: int | None = None
    seed: int = 42
    embargo_bars: int = 1
    out_of_sample_ratio: float = 0.3
    folds: int = 4
    train_ratio: float = 0.5
    holdout_ratio: float = 0.2

    @classmethod
    def from_payload(cls, mode: str, payload: dict[str, Any]) -> "OptimizationSettings":
        if mode not in MODES:
            raise OptimizationRequestError(f"mode must be one of: {', '.join(MODES)}")
        objective = str(payload.get("objective") or "sharpeRatio")
        if objective not in OBJECTIVES:
            raise OptimizationRequestError(f"objective must be one of: {', '.join(OBJECTIVES)}")
        values: dict[str, Any] = {
            "mode": mode,
            "objective": objective,
            "max_combinations": _int_field(payload, "maxCombinations", None, 1, None),
            "seed": _int_field(payload, "seed", 42, 0, 2**31 - 1),
            "embargo_bars": _int_field(payload, "embargoBars", 1, 0, 1000),
        }
        if mode == MODE_SWEEP:
            values["out_of_sample_ratio"] = _float_field(payload, "outOfSampleRatio", 0.3, 0.1, 0.5)
        else:
            values["folds"] = _int_field(payload, "folds", 4, 2, 10)
            values["train_ratio"] = _float_field(payload, "trainRatio", 0.5, 0.3, 0.85)
            values["holdout_ratio"] = _float_field(payload, "holdoutRatio", 0.2, 0.1, 0.4)
        return cls(**values)

    def public(self) -> dict[str, Any]:
        output: dict[str, Any] = {
            "objective": self.objective,
            "maxCombinations": self.max_combinations,
            "seed": self.seed,
            "embargoBars": self.embargo_bars,
        }
        if self.mode == MODE_SWEEP:
            output["outOfSampleRatio"] = self.out_of_sample_ratio
        else:
            output.update({
                "folds": self.folds,
                "trainRatio": self.train_ratio,
                "holdoutRatio": self.holdout_ratio,
            })
        return output


@dataclass(frozen=True)
class PreparedStudy:
    """Validated parameters and the combination budget of one study."""

    code: str
    parameters: tuple[SearchParameter, ...]
    constraints: tuple[ParameterConstraint, ...]
    settings: OptimizationSettings
    search_space_size: int
    combinations: int

    def candidates(self) -> tuple[list[dict[str, Any]], int]:
        sampler = ParameterSampler(list(self.parameters), seed=self.settings.seed)
        candidates, stats = sampler.grid_with_stats(
            self.combinations,
            accept=lambda params: first_rejection(self.constraints, params) is None,
        )
        return candidates, int(stats.get("rejected") or 0)


def prepare_study(
    *,
    source: dict[str, Any],
    code: str,
    mode: str,
    payload: dict[str, Any],
    limits: OptimizationLimits,
) -> PreparedStudy:
    """Validate the requested space against the source declaration and limits.

    ``source`` supplies the declared parameter schema; ``code`` is the pinned
    version body. Raises ``OptimizationRequestError`` for contract violations.
    """
    settings = OptimizationSettings.from_payload(mode, payload)
    raw_space = payload.get("parameterSpace")
    if raw_space is not None and (
        not isinstance(raw_space, list) or not all(isinstance(item, dict) for item in raw_space)
    ):
        raise OptimizationRequestError("parameterSpace must be an array of objects")
    declared_source = {**source, "code": code}
    if not build_parameter_space(declared_source).get("parameters"):
        raise OptimizationRequestError("Strategy source declares no optimizable parameters")
    try:
        rows = StrategyEvolutionService._parameter_rows(declared_source, raw_space)
        parameters = tuple(SearchParameter.from_payload(item) for item in rows)
    except (TypeError, ValueError) as exc:
        raise OptimizationRequestError(str(exc)) from None
    if not parameters:
        raise OptimizationRequestError("strategyEvolution.parametersRequired")
    if len(parameters) > 8:
        raise OptimizationRequestError("strategyEvolution.parameterLimitExceeded")
    adapted = adapt_source_for_parameters(code, rows)
    constraints = discover_parameter_constraints(adapted, {item.name for item in parameters})
    space = ParameterSampler(list(parameters), seed=settings.seed).space_size()
    requested = settings.max_combinations
    combinations = min(space, requested) if requested else space
    if combinations > limits.max_combinations:
        raise OptimizationRequestError(
            "Optimization combination limit exceeded",
            details={
                "error_type": "OPTIMIZATION_COMBINATION_LIMIT",
                "limit": limits.max_combinations,
                "requested": combinations,
                "searchSpaceSize": space,
                "hint": "Narrow parameterSpace ranges, increase step, or set maxCombinations within the limit",
            },
        )
    return PreparedStudy(
        code=adapted,
        parameters=parameters,
        constraints=constraints,
        settings=settings,
        search_space_size=space,
        combinations=combinations,
    )


def run_study(
    study: PreparedStudy,
    *,
    evaluator: Evaluator,
    start_date: datetime,
    end_date: datetime,
    commission: float,
    slippage: float,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    candidates, rejected = study.candidates()
    if not candidates:
        raise StrategyEvolutionFailure(
            "parameterOptimization.noCandidates",
            {"constraintRejected": rejected, "searchSpaceSize": study.search_space_size},
        )
    context_provider = getattr(evaluator, "walk_forward_context", None)
    context = context_provider(start_date, end_date) if callable(context_provider) else {}
    runner = _run_sweep if study.settings.mode == MODE_SWEEP else _run_walk_forward
    result = runner(
        study,
        candidates,
        evaluator=evaluator,
        context=context,
        start_date=start_date,
        end_date=end_date,
        commission=commission,
        slippage=slippage,
        on_progress=on_progress,
    )
    result["combinations"] = {
        "searchSpaceSize": study.search_space_size,
        "requested": study.combinations,
        "evaluated": len(candidates),
        "constraintRejected": rejected,
        "coveragePercent": round(len(candidates) / study.search_space_size * 100.0, 4)
        if study.search_space_size
        else 0.0,
    }
    result["settings"] = study.settings.public()
    result["history"] = _persist_history(evaluator, result, commission, slippage)
    result.pop("_historyWindows", None)
    return result


def public_metrics(result: dict[str, Any]) -> dict[str, float]:
    snapshot = metric_snapshot(result or {})
    return {
        "totalReturn": round(snapshot["return"], 6),
        "sharpeRatio": round(snapshot["sharpe"], 6),
        "maxDrawdown": round(snapshot["drawdown"], 6),
        "winRate": round(snapshot["winRate"], 6),
        "totalTrades": int(snapshot["trades"]),
        "activity": int(snapshot["activity"]),
        "bars": int(snapshot["samples"]),
    }


def _eligible(metrics: dict[str, float], result: dict[str, Any]) -> bool:
    return metric_snapshot(result or {})["valid"] > 0 and metrics["activity"] > 0


def _rank_key(metrics: dict[str, float], objective: str) -> tuple[float, float]:
    primary = metrics["sharpeRatio"] if objective == "sharpeRatio" else metrics["totalReturn"]
    secondary = metrics["totalReturn"] if objective == "sharpeRatio" else metrics["sharpeRatio"]
    return float(primary), float(secondary)


def _window(start: datetime, end: datetime, bars: int) -> dict[str, Any]:
    return {"start": start.isoformat(), "end": end.isoformat(), "bars": bars}


def _degradation(in_sample: dict[str, float], out_of_sample: dict[str, float]) -> dict[str, float]:
    return {
        "totalReturnDelta": round(out_of_sample["totalReturn"] - in_sample["totalReturn"], 6),
        "sharpeRatioDelta": round(out_of_sample["sharpeRatio"] - in_sample["sharpeRatio"], 6),
    }


def _emit(on_progress: ProgressCallback | None, event: dict[str, Any]) -> None:
    if on_progress:
        on_progress(event)


def _split_observations(
    start: datetime,
    end: datetime,
    observations: Sequence[datetime] | None,
    *,
    out_of_sample_ratio: float,
    embargo_bars: int,
) -> dict[str, Any]:
    timestamps = _observation_times(_as_datetime(start), _as_datetime(end), observations)
    total = len(timestamps)
    out_bars = int(total * out_of_sample_ratio)
    in_bars = total - out_bars - embargo_bars
    if out_bars < MIN_OUT_OF_SAMPLE_BARS or in_bars < MIN_IN_SAMPLE_BARS:
        raise StrategyEvolutionFailure(
            "parameterOptimization.rangeTooShort",
            {
                "observations": total,
                "minimumInSampleBars": MIN_IN_SAMPLE_BARS,
                "minimumOutOfSampleBars": MIN_OUT_OF_SAMPLE_BARS,
                "embargoBars": embargo_bars,
            },
        )
    oos_index = total - out_bars
    return {
        "inSample": (timestamps[0], timestamps[in_bars - 1], in_bars),
        "outOfSample": (timestamps[oos_index], timestamps[-1], out_bars),
        "observations": total,
    }


def _run_sweep(
    study: PreparedStudy,
    candidates: list[dict[str, Any]],
    *,
    evaluator: Evaluator,
    context: dict[str, Any],
    start_date: datetime,
    end_date: datetime,
    commission: float,
    slippage: float,
    on_progress: ProgressCallback | None,
) -> dict[str, Any]:
    settings = study.settings
    split = _split_observations(
        start_date,
        end_date,
        context.get("observations"),
        out_of_sample_ratio=settings.out_of_sample_ratio,
        embargo_bars=settings.embargo_bars,
    )
    is_start, is_end, is_bars = split["inSample"]
    oos_start, oos_end, oos_bars = split["outOfSample"]
    ranked: list[tuple[tuple[float, float], dict[str, Any], dict[str, float]]] = []
    for index, params in enumerate(candidates, start=1):
        result = evaluator(params, is_start, is_end, commission, slippage)
        metrics = public_metrics(result)
        if _eligible(metrics, result):
            ranked.append((_rank_key(metrics, settings.objective), params, metrics))
        _emit(on_progress, {"phase": "inSample", "completed": index, "total": len(candidates)})
    if not ranked:
        raise StrategyEvolutionFailure(
            "parameterOptimization.noActiveCandidates",
            {"evaluated": len(candidates), "window": "inSample"},
        )
    ranked.sort(key=lambda row: row[0], reverse=True)
    _key, best_params, best_in_sample = ranked[0]
    _emit(on_progress, {"phase": "outOfSample", "completed": 0, "total": 1})
    out_result = evaluator(best_params, oos_start, oos_end, commission, slippage)
    best_out_of_sample = public_metrics(out_result)
    return {
        "mode": MODE_SWEEP,
        "objective": settings.objective,
        "plan": {
            "observations": split["observations"],
            "embargoBars": settings.embargo_bars,
            "frequency": context.get("frequency"),
            "inSample": _window(is_start, is_end, is_bars),
            "outOfSample": _window(oos_start, oos_end, oos_bars),
        },
        "bestParams": best_params,
        "inSample": {
            "window": _window(is_start, is_end, is_bars),
            "usedForSelection": True,
            "eligibleCandidates": len(ranked),
            "best": best_in_sample,
            "topCandidates": [
                {"rank": rank, "params": params, "metrics": metrics}
                for rank, (_k, params, metrics) in enumerate(ranked[:TOP_CANDIDATES], start=1)
            ],
        },
        "outOfSample": {
            "window": _window(oos_start, oos_end, oos_bars),
            "usedForSelection": False,
            "best": best_out_of_sample,
        },
        "bestParamsOutOfSample": {
            "available": True,
            "window": _window(oos_start, oos_end, oos_bars),
            "metrics": best_out_of_sample,
            "passed": best_out_of_sample["activity"] > 0 and best_out_of_sample["totalReturn"] > 0,
            "degradation": _degradation(best_in_sample, best_out_of_sample),
        },
        "_historyWindows": {
            "params": best_params,
            "inSample": (is_start, is_end),
            "outOfSample": (oos_start, oos_end),
        },
    }


def _fold_pairs(
    evaluator: Evaluator,
    params: dict[str, Any],
    folds: tuple[WalkForwardFold, ...],
    commission: float,
    slippage: float,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    evaluate_plan = getattr(evaluator, "evaluate_plan", None)
    if callable(evaluate_plan):
        return list(evaluate_plan(params, folds, commission, slippage))
    return [
        (
            evaluator(params, fold.train_start, fold.train_end, commission, slippage),
            evaluator(params, fold.validation_start, fold.validation_end, commission, slippage),
        )
        for fold in folds
    ]


def _compounded(values: Sequence[float]) -> float:
    growth = 1.0
    for value in values:
        if math.isfinite(value):
            growth *= max(0.0, 1.0 + value / 100.0)
    return round((growth - 1.0) * 100.0, 6)


def _run_walk_forward(
    study: PreparedStudy,
    candidates: list[dict[str, Any]],
    *,
    evaluator: Evaluator,
    context: dict[str, Any],
    start_date: datetime,
    end_date: datetime,
    commission: float,
    slippage: float,
    on_progress: ProgressCallback | None,
) -> dict[str, Any]:
    settings = study.settings
    plan = build_walk_forward_plan(
        start_date,
        end_date,
        folds=settings.folds,
        train_ratio=settings.train_ratio,
        blind_ratio=settings.holdout_ratio,
        observations=context.get("observations"),
        warmup_bars=int(context.get("warmupBars") or 0),
        embargo_bars=settings.embargo_bars,
    )
    folds = plan.folds
    dev_start = folds[0].train_start
    dev_end = folds[-1].validation_end
    # Per fold: (rank key, params, train metrics, validation metrics) of the training winner.
    fold_winners: list[tuple[tuple[float, float], dict[str, Any], dict[str, float], dict[str, float]] | None]
    fold_winners = [None] * len(folds)
    development: list[tuple[tuple[float, float], dict[str, Any], dict[str, float]]] = []
    for index, params in enumerate(candidates, start=1):
        pairs = _fold_pairs(evaluator, params, folds, commission, slippage)
        for fold_index, (train_result, validation_result) in enumerate(pairs):
            train_metrics = public_metrics(train_result)
            if not _eligible(train_metrics, train_result):
                continue
            key = _rank_key(train_metrics, settings.objective)
            current = fold_winners[fold_index]
            if current is None or key > current[0]:
                fold_winners[fold_index] = (key, params, train_metrics, public_metrics(validation_result))
        dev_result = evaluator(params, dev_start, dev_end, commission, slippage)
        dev_metrics = public_metrics(dev_result)
        if _eligible(dev_metrics, dev_result):
            development.append((_rank_key(dev_metrics, settings.objective), params, dev_metrics))
        _emit(on_progress, {"phase": "inSample", "completed": index, "total": len(candidates)})
    if not development or all(winner is None for winner in fold_winners):
        raise StrategyEvolutionFailure(
            "parameterOptimization.noActiveCandidates",
            {"evaluated": len(candidates), "window": "inSample"},
        )
    development.sort(key=lambda row: row[0], reverse=True)
    _key, best_params, best_in_sample = development[0]

    in_sample_folds = []
    out_of_sample_folds = []
    for fold, winner in zip(folds, fold_winners):
        if winner is None:
            in_sample_folds.append({
                "index": fold.index,
                "window": _window(fold.train_start, fold.train_end, fold.train_bars),
                "params": None,
                "metrics": None,
            })
            out_of_sample_folds.append({
                "index": fold.index,
                "window": _window(fold.validation_start, fold.validation_end, fold.validation_bars),
                "params": None,
                "metrics": None,
                "skippedReason": "noActiveTrainingCandidate",
            })
            continue
        _rank, params, train_metrics, validation_metrics = winner
        in_sample_folds.append({
            "index": fold.index,
            "window": _window(fold.train_start, fold.train_end, fold.train_bars),
            "params": params,
            "metrics": train_metrics,
        })
        out_of_sample_folds.append({
            "index": fold.index,
            "window": _window(fold.validation_start, fold.validation_end, fold.validation_bars),
            "params": params,
            "metrics": validation_metrics,
        })
    validated = [row["metrics"] for row in out_of_sample_folds if row["metrics"]]
    stitched = {
        "compoundedReturn": _compounded([row["totalReturn"] for row in validated]),
        "meanSharpeRatio": round(sum(row["sharpeRatio"] for row in validated) / len(validated), 6),
        "worstDrawdown": round(min(row["maxDrawdown"] for row in validated), 6),
        "totalTrades": sum(row["totalTrades"] for row in validated),
        "positiveFolds": sum(1 for row in validated if row["totalReturn"] > 0),
        "validatedFolds": len(validated),
        "folds": len(folds),
    }
    winner_identities = {
        tuple(sorted((str(k), repr(v)) for k, v in winner[1].items()))
        for winner in fold_winners
        if winner is not None
    }
    best_identity = tuple(sorted((str(k), repr(v)) for k, v in best_params.items()))
    stability = {
        "distinctFoldWinners": len(winner_identities),
        "foldsMatchingBestParams": sum(
            1
            for winner in fold_winners
            if winner is not None
            and tuple(sorted((str(k), repr(v)) for k, v in winner[1].items())) == best_identity
        ),
    }

    holdout = None
    if plan.blind_start and plan.blind_end:
        _emit(on_progress, {"phase": "outOfSample", "completed": 0, "total": 1})
        holdout_metrics = public_metrics(
            evaluator(best_params, plan.blind_start, plan.blind_end, commission, slippage)
        )
        holdout_window = _window(plan.blind_start, plan.blind_end, plan.blind_bars)
        holdout = {
            "available": True,
            "window": holdout_window,
            "metrics": holdout_metrics,
            "passed": holdout_metrics["activity"] > 0 and holdout_metrics["totalReturn"] > 0,
            "degradation": _degradation(best_in_sample, holdout_metrics),
        }
    best_out_of_sample = holdout or {
        "available": False,
        "reason": plan.blind_omitted_reason or "holdoutUnavailable",
    }
    return {
        "mode": MODE_WALK_FORWARD,
        "objective": settings.objective,
        "plan": {**plan.metadata(), "frequency": context.get("frequency")},
        "bestParams": best_params,
        "inSample": {
            "window": _window(dev_start, dev_end, plan.development_bars),
            "usedForSelection": True,
            "eligibleCandidates": len(development),
            "best": best_in_sample,
            "folds": in_sample_folds,
            "topCandidates": [
                {"rank": rank, "params": params, "metrics": metrics}
                for rank, (_k, params, metrics) in enumerate(development[:TOP_CANDIDATES], start=1)
            ],
        },
        "outOfSample": {
            "usedForSelection": False,
            "folds": out_of_sample_folds,
            "summary": stitched,
            "holdout": holdout,
        },
        "bestParamsOutOfSample": best_out_of_sample,
        "parameterStability": stability,
        "_historyWindows": {
            "params": best_params,
            "inSample": (dev_start, dev_end),
            "outOfSample": (plan.blind_start, plan.blind_end) if holdout else None,
        },
    }


def _persist_history(
    evaluator: Evaluator,
    result: dict[str, Any],
    commission: float,
    slippage: float,
) -> dict[str, Any]:
    """Save the best parameters' in-sample and out-of-sample runs to history."""
    windows = result.get("_historyWindows") or {}
    persist = getattr(evaluator, "persist_run", None)
    history: dict[str, Any] = {"inSampleRunId": None, "outOfSampleRunId": None, "persisted": False}
    if not callable(persist) or not windows:
        return history
    try:
        for key, field in (("inSample", "inSampleRunId"), ("outOfSample", "outOfSampleRunId")):
            window = windows.get(key)
            if window and window[0] and window[1]:
                history[field] = persist(windows["params"], window[0], window[1], commission, slippage)
        history["persisted"] = True
    except Exception:
        # The study itself succeeded; report the missing history explicitly
        # instead of failing the job after all candidates were evaluated.
        logger.exception("Persisting optimization history runs failed")
        history["error"] = "historyPersistFailed"
    return history
