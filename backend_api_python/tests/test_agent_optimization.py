"""Agent parameter sweep and walk-forward optimization jobs (#30)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.services.strategy_v2 as strategy_v2_pkg
from app.routes.agent_v1 import backtests, optimizations
from app.services.strategy_evolution import agent_optimization as engine
from app.services.strategy_evolution.engine import StrategyEvolutionFailure
from app.services.strategy_v2.models import (
    InstrumentSpec, StrategyManifest, SubscriptionSpec, UniverseSpec,
)
from app.utils import agent_auth

OWNER = 5
OTHER_TENANT = 9
CODE = "def handle_data(context, data):\n    pass\n"
PARAM_SCHEMA = {"params": [
    {"name": "fast", "type": "int", "min": 5, "max": 20, "step": 5},
    {"name": "slow", "type": "int", "min": 30, "max": 60, "step": 10},
]}
HEADERS = {"Authorization": "Bearer qd_agent_OPTTEST1234", "Idempotency-Key": "opt-test"}
DATES = {"startDate": "2024-01-01", "endDate": "2024-12-31"}
LIMITS = engine.OptimizationLimits(max_combinations=50, max_concurrent_per_tenant=1, max_daily_per_tenant=3)


# --------------------------------------------------------------------------- engine


def _days(count: int, start: datetime = datetime(2024, 1, 1)) -> tuple[datetime, ...]:
    return tuple(start + timedelta(days=index) for index in range(count))


class FakeEvaluator:
    """Deterministic evaluator whose return depends on params and window."""

    def __init__(self, score, observations):
        self.score = score
        self.observations = observations
        self.calls: list[tuple[dict, datetime, datetime]] = []
        self.persisted: list[tuple[dict, datetime, datetime]] = []

    def walk_forward_context(self, _start, _end):
        return {"observations": self.observations, "warmupBars": 0, "frequency": "1d"}

    def __call__(self, params, start, end, commission, slippage):
        self.calls.append((dict(params), start, end))
        value = float(self.score(params, start, end))
        return {
            "totalReturn": value,
            "sharpeRatio": value / 10.0,
            "maxDrawdown": -abs(value) / 2.0,
            "winRate": 50.0,
            "totalTrades": 4,
            "totalExecutions": 8,
            "sampleCount": max(1, (end - start).days + 1),
        }

    def persist_run(self, params, start, end, commission, slippage):
        self.persisted.append((dict(params), start, end))
        return 1000 + len(self.persisted)


def _study(mode: str, payload: dict | None = None, limits=LIMITS) -> engine.PreparedStudy:
    source = {"id": 11, "name": "Mine", "param_schema": PARAM_SCHEMA, "metadata": {}}
    return engine.prepare_study(source=source, code=CODE, mode=mode, payload=payload or {}, limits=limits)


def test_prepare_study_uses_declared_space_and_counts_combinations():
    study = _study(engine.MODE_SWEEP)
    assert {item.name for item in study.parameters} == {"fast", "slow"}
    assert study.search_space_size == 16
    assert study.combinations == 16
    assert len(study.candidates()[0]) == 16


@pytest.mark.parametrize(("space", "message"), [
    ([{"name": "unknown", "min": 1, "max": 2, "step": 1}], "strategyEvolution.parameterNotDeclared"),
    ([{"name": "fast", "min": 1, "max": 20, "step": 5}], "strategyEvolution.parameterRangeOutsideDeclaration"),
    ([{"name": "fast", "min": 5, "max": 50, "step": 5}], "strategyEvolution.parameterRangeOutsideDeclaration"),
    ("fast", "parameterSpace must be an array of objects"),
], ids=["undeclared", "below-min", "above-max", "not-a-list"])
def test_prepare_study_rejects_space_outside_declaration(space, message):
    with pytest.raises(engine.OptimizationRequestError, match=message):
        _study(engine.MODE_SWEEP, {"parameterSpace": space})


def test_prepare_study_enforces_combination_limit_with_details():
    tight = engine.OptimizationLimits(max_combinations=10, max_concurrent_per_tenant=1, max_daily_per_tenant=1)
    with pytest.raises(engine.OptimizationRequestError) as caught:
        _study(engine.MODE_SWEEP, limits=tight)
    assert caught.value.details["error_type"] == "OPTIMIZATION_COMBINATION_LIMIT"
    assert caught.value.details["limit"] == 10
    assert caught.value.details["requested"] == 16

    sampled = _study(engine.MODE_SWEEP, {"maxCombinations": 8}, limits=tight)
    assert sampled.combinations == 8
    assert len(sampled.candidates()[0]) == 8


def test_prepare_study_narrowed_space_fits_limit():
    tight = engine.OptimizationLimits(max_combinations=4, max_concurrent_per_tenant=1, max_daily_per_tenant=1)
    study = _study(engine.MODE_SWEEP, {"parameterSpace": [
        {"name": "fast", "min": 5, "max": 10, "step": 5},
        {"name": "slow", "min": 30, "max": 40, "step": 10},
    ]}, limits=tight)
    assert study.combinations == 4


@pytest.mark.parametrize(("mode", "payload", "message"), [
    (engine.MODE_SWEEP, {"objective": "profit"}, "objective must be one of"),
    (engine.MODE_SWEEP, {"outOfSampleRatio": 0.9}, "outOfSampleRatio"),
    (engine.MODE_WALK_FORWARD, {"folds": 1}, "folds"),
    (engine.MODE_WALK_FORWARD, {"holdoutRatio": 0}, "holdoutRatio"),
    (engine.MODE_SWEEP, {"maxCombinations": 0}, "maxCombinations"),
])
def test_settings_reject_out_of_range_values(mode, payload, message):
    with pytest.raises(engine.OptimizationRequestError, match=message):
        _study(mode, payload)


def test_sweep_selects_in_sample_and_validates_best_only_out_of_sample():
    observations = _days(200)
    oos_cutoff = observations[140]

    def score(params, start, end):
        if start >= oos_cutoff:
            # Out of sample the ranking flips; it must not influence selection.
            return 50.0 - params["fast"]
        return float(params["fast"] + params["slow"] / 100.0)

    evaluator = FakeEvaluator(score, observations)
    result = engine.run_study(
        _study(engine.MODE_SWEEP),
        evaluator=evaluator,
        start_date=observations[0],
        end_date=observations[-1],
        commission=0.001,
        slippage=0.0,
    )

    assert result["mode"] == "sweep"
    assert result["bestParams"] == {"fast": 20, "slow": 60}
    assert result["inSample"]["usedForSelection"] is True
    assert result["outOfSample"]["usedForSelection"] is False
    assert result["inSample"]["best"]["totalReturn"] == pytest.approx(20.6)
    assert result["outOfSample"]["best"]["totalReturn"] == pytest.approx(30.0)
    assert result["bestParamsOutOfSample"]["available"] is True
    assert result["bestParamsOutOfSample"]["passed"] is True
    assert result["bestParamsOutOfSample"]["degradation"]["totalReturnDelta"] == pytest.approx(9.4)
    assert result["inSample"]["topCandidates"][0]["params"] == result["bestParams"]
    assert result["combinations"]["evaluated"] == 16

    oos_calls = [call for call in evaluator.calls if call[1] >= oos_cutoff]
    assert [call[0] for call in oos_calls] == [{"fast": 20, "slow": 60}]
    is_end = datetime.fromisoformat(result["plan"]["inSample"]["end"])
    oos_start = datetime.fromisoformat(result["plan"]["outOfSample"]["start"])
    assert is_end < oos_start
    assert all(call[2] <= is_end for call in evaluator.calls if call[1] < oos_cutoff)

    assert result["history"] == {"inSampleRunId": 1001, "outOfSampleRunId": 1002, "persisted": True}
    assert [row[0] for row in evaluator.persisted] == [{"fast": 20, "slow": 60}] * 2
    assert "_historyWindows" not in result


def test_sweep_rejects_range_too_short():
    observations = _days(20)
    evaluator = FakeEvaluator(lambda *_: 1.0, observations)
    with pytest.raises(StrategyEvolutionFailure, match="rangeTooShort"):
        engine.run_study(
            _study(engine.MODE_SWEEP), evaluator=evaluator,
            start_date=observations[0], end_date=observations[-1], commission=0.0, slippage=0.0,
        )


def test_sweep_without_active_candidates_fails_with_details():
    observations = _days(200)
    evaluator = FakeEvaluator(lambda *_: 1.0, observations)
    original = evaluator.__call__

    def inactive(params, start, end, commission, slippage):
        return {**original(params, start, end, commission, slippage), "totalTrades": 0, "totalExecutions": 0}

    with pytest.raises(StrategyEvolutionFailure) as caught:
        engine.run_study(
            _study(engine.MODE_SWEEP), evaluator=inactive,
            start_date=observations[0], end_date=observations[-1], commission=0.0, slippage=0.0,
        )
    assert caught.value.details["evaluated"] == 16


def test_walk_forward_selects_per_fold_on_training_and_validates_holdout():
    observations = _days(400)
    holdout_cutoff = observations[400 - 80]

    def score(params, start, end):
        if start >= holdout_cutoff:
            return 7.0
        if start == observations[0]:
            # Training (anchored) windows prefer the fastest setting.
            return float(params["fast"] + params["slow"] / 1000.0)
        # Validation windows prefer the slowest; they must not drive selection.
        return float(100 - params["fast"])

    evaluator = FakeEvaluator(score, observations)
    progress: list[dict] = []
    result = engine.run_study(
        _study(engine.MODE_WALK_FORWARD, {"folds": 3, "holdoutRatio": 0.2, "objective": "totalReturn"}),
        evaluator=evaluator,
        start_date=observations[0],
        end_date=observations[-1],
        commission=0.0,
        slippage=0.0,
        on_progress=progress.append,
    )

    assert result["mode"] == "walkForward"
    assert result["bestParams"] == {"fast": 20, "slow": 60}
    in_folds = result["inSample"]["folds"]
    out_folds = result["outOfSample"]["folds"]
    assert len(in_folds) == len(out_folds) == len(result["plan"]["folds"]) >= 2
    assert all(row["params"] == {"fast": 20, "slow": 60} for row in in_folds)
    assert all(row["metrics"]["totalReturn"] == pytest.approx(80.0) for row in out_folds)
    assert result["outOfSample"]["usedForSelection"] is False
    assert result["outOfSample"]["summary"]["validatedFolds"] == len(out_folds)
    assert result["outOfSample"]["summary"]["positiveFolds"] == len(out_folds)

    best_oos = result["bestParamsOutOfSample"]
    assert best_oos["available"] is True
    assert best_oos["metrics"]["totalReturn"] == pytest.approx(7.0)
    assert result["outOfSample"]["holdout"] == best_oos
    holdout_calls = [call for call in evaluator.calls if call[1] >= holdout_cutoff]
    assert [call[0] for call in holdout_calls] == [{"fast": 20, "slow": 60}]
    assert result["parameterStability"] == {"distinctFoldWinners": 1, "foldsMatchingBestParams": len(in_folds)}
    assert result["history"]["outOfSampleRunId"] == 1002
    assert progress[-1]["phase"] == "outOfSample"


# --------------------------------------------------------------------------- routes


def _manifest() -> StrategyManifest:
    instrument = InstrumentSpec(market="Crypto", symbol="BTC/USDT")
    return StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cta",
        universe=UniverseSpec(kind="static", instruments=(instrument,)),
        subscriptions=(SubscriptionSpec(instruments=(instrument,), frequency="1d"),),
        schedules=(),
        handlers=("handle_data",),
    )


class FakeSources:
    def __init__(self):
        self.sources = {
            11: {"id": 11, "user_id": OWNER, "name": "Mine", "metadata": {}, "param_schema": PARAM_SCHEMA},
            12: {"id": 12, "user_id": OTHER_TENANT, "name": "Theirs", "metadata": {}, "param_schema": PARAM_SCHEMA},
            15: {"id": 15, "user_id": OWNER, "name": "Bare", "metadata": {}},
        }
        self.versions = [
            {"id": 101, "source_id": 11, "user_id": OWNER, "version_no": 1, "code": CODE},
            {"id": 201, "source_id": 12, "user_id": OTHER_TENANT, "version_no": 1, "code": CODE},
            {"id": 501, "source_id": 15, "user_id": OWNER, "version_no": 1, "code": CODE},
        ]

    def get_source(self, source_id, user_id=None):
        row = self.sources.get(int(source_id))
        if not row or (user_id is not None and row["user_id"] != int(user_id)):
            return None
        return dict(row)

    def get_latest_version(self, source_id, user_id):
        rows = [v for v in self.versions if v["source_id"] == int(source_id) and v["user_id"] == int(user_id)]
        return dict(max(rows, key=lambda v: v["version_no"])) if rows else None

    def get_version_by_number(self, source_id, version_no, user_id):
        rows = [v for v in self.versions if v["source_id"] == int(source_id)
                and v["user_id"] == int(user_id) and v["version_no"] == int(version_no)]
        return dict(rows[0]) if rows else None

    def get_version(self, version_id, user_id):
        rows = [v for v in self.versions if v["id"] == int(version_id) and v["user_id"] == int(user_id)]
        return dict(rows[0]) if rows else None


@pytest.fixture
def sources(monkeypatch):
    fake = FakeSources()
    monkeypatch.setattr(backtests, "get_script_source_service", lambda: fake)
    monkeypatch.setattr(optimizations, "get_script_source_service", lambda: fake)
    return fake


@pytest.fixture
def compiled(monkeypatch):
    compile_mock = Mock(return_value=SimpleNamespace(manifest=_manifest()))
    monkeypatch.setattr(strategy_v2_pkg, "compile_strategy_v2", compile_mock)
    return compile_mock


@pytest.fixture
def gateway(monkeypatch):
    state = {"scopes": "R,B", "active": 0, "daily": 0, "counts": []}
    token = {"id": 7, "user_id": OWNER, "markets": "*", "instruments": "*",
             "status": "active", "rate_limit_per_min": 100, "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: {**token, "scopes": state["scopes"]})
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(agent_auth, "_reserve_idempotency", lambda *_: ("reserved", None))
    monkeypatch.setattr(agent_auth, "_complete_idempotency", lambda *_: None)
    monkeypatch.setattr(optimizations, "optimization_limits", lambda: LIMITS)

    def count_active(**kwargs):
        state["counts"].append(kwargs)
        return state["active"] if kwargs.get("kind") == optimizations.OPTIMIZATION_JOB_KIND else 0

    def count_daily(**kwargs):
        state["daily_query"] = kwargs
        return state["daily"]

    monkeypatch.setattr(optimizations, "count_active_jobs", count_active)
    monkeypatch.setattr(optimizations, "count_jobs_created_since", count_daily)

    @contextmanager
    def no_existing(_):
        yield None

    monkeypatch.setattr(optimizations, "with_idempotency", no_existing)
    submit = Mock(return_value={"job_id": "job-1", "status": "queued", "kind": "parameter_optimization"})
    monkeypatch.setattr(optimizations, "submit_job", submit)
    state["submit"] = submit
    yield state
    agent_auth._rate_state.clear()


def _post(client, path, body, headers=HEADERS):
    return client.post(f"/api/agent/v1/optimization/{path}", json={**DATES, "sourceId": 11, **body}, headers=headers)


def test_sweep_submission_queues_pinned_job(client, gateway, sources, compiled):
    response = _post(client, "sweep", {"objective": "totalReturn", "outOfSampleRatio": 0.25})

    assert response.status_code == 202
    data = response.get_json()["data"]
    assert data["mode"] == "sweep"
    assert data["combinations"] == 16
    assert data["strategySource"] == {"sourceId": 11, "versionId": 101, "versionNo": 1}
    assert data["limits"] == LIMITS.public()
    kwargs = gateway["submit"].call_args.kwargs
    assert kwargs["kind"] == "parameter_optimization"
    assert kwargs["idempotency_key"] == "opt-test"
    assert kwargs["runner"] is optimizations._run_optimization
    payload = kwargs["request_payload"]
    assert payload[optimizations.MODE_KEY] == "sweep"
    assert payload[backtests.SOURCE_REF_KEY]["versionId"] == 101
    assert payload["__user_id"] == OWNER
    assert "code" not in payload


def test_walk_forward_submission_uses_walk_forward_mode(client, gateway, sources, compiled):
    response = _post(client, "walk-forward", {"folds": 5, "holdoutRatio": 0.15})

    assert response.status_code == 202
    assert response.get_json()["data"]["mode"] == "walkForward"
    assert gateway["submit"].call_args.kwargs["request_payload"][optimizations.MODE_KEY] == "walkForward"


def test_submission_requires_idempotency_key(client, gateway, sources, compiled):
    headers = {"Authorization": HEADERS["Authorization"]}
    response = _post(client, "sweep", {}, headers=headers)

    assert response.status_code == 400
    assert "Idempotency-Key" in response.get_json()["message"]
    gateway["submit"].assert_not_called()


def test_submission_requires_backtest_scope(client, gateway, sources, compiled):
    gateway["scopes"] = "R"
    response = _post(client, "walk-forward", {})

    assert response.status_code == 403
    gateway["submit"].assert_not_called()


@pytest.mark.parametrize(("path", "body", "message"), [
    ("sweep", {"folds": 4}, "Unsupported optimization fields: folds"),
    ("walk-forward", {"outOfSampleRatio": 0.3}, "Unsupported optimization fields: outOfSampleRatio"),
    ("sweep", {"code": CODE}, "Unsupported optimization fields: code"),
    ("sweep", {backtests.SOURCE_REF_KEY: {"sourceId": 12}}, "Unsupported optimization fields"),
    ("sweep", {"commission": 5}, "commission must be a number between 0 and 1"),
    ("sweep", {"parameterSpace": [{"name": "nope", "min": 1, "max": 2, "step": 1}]},
     "strategyEvolution.parameterNotDeclared"),
])
def test_invalid_requests_are_rejected(client, gateway, sources, compiled, path, body, message):
    response = _post(client, path, body)

    assert response.status_code == 400
    assert response.get_json()["message"].startswith(message)
    gateway["submit"].assert_not_called()


def test_source_id_is_required(client, gateway, sources, compiled):
    response = client.post("/api/agent/v1/optimization/sweep", json=DATES, headers=HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "sourceId is required"


def test_other_tenant_source_is_404(client, gateway, sources, compiled):
    response = _post(client, "sweep", {"sourceId": 12})

    assert response.status_code == 404
    gateway["submit"].assert_not_called()


def test_source_without_declared_parameters_is_rejected(client, gateway, sources, compiled):
    response = _post(client, "sweep", {"sourceId": 15})

    assert response.status_code == 400
    assert "no optimizable parameters" in response.get_json()["message"]


def test_combination_limit_is_explicit(client, gateway, sources, compiled, monkeypatch):
    tight = engine.OptimizationLimits(max_combinations=10, max_concurrent_per_tenant=1, max_daily_per_tenant=3)
    monkeypatch.setattr(optimizations, "optimization_limits", lambda: tight)
    response = _post(client, "sweep", {})

    assert response.status_code == 400
    body = response.get_json()
    assert body["message"] == "Optimization combination limit exceeded"
    assert body["details"]["error_type"] == "OPTIMIZATION_COMBINATION_LIMIT"
    assert body["details"]["limit"] == 10
    assert body["details"]["searchSpaceSize"] == 16
    gateway["submit"].assert_not_called()


def test_concurrency_limit_is_explicit(client, gateway, sources, compiled):
    gateway["active"] = 1
    response = _post(client, "walk-forward", {})

    assert response.status_code == 429
    body = response.get_json()
    assert body["details"]["error_type"] == "OPTIMIZATION_CONCURRENCY_LIMIT"
    assert body["details"]["limit"] == 1
    assert body["retriable"] is True
    gateway["submit"].assert_not_called()


def test_daily_limit_is_explicit(client, gateway, sources, compiled):
    gateway["daily"] = 3
    response = _post(client, "sweep", {})

    assert response.status_code == 429
    body = response.get_json()
    assert body["message"] == "Optimization daily limit reached"
    assert body["details"]["error_type"] == "OPTIMIZATION_DAILY_LIMIT"
    assert body["details"]["used"] == 3
    assert body["details"]["resetAt"].endswith("T00:00:00Z")
    assert body["retriable"] is False
    assert gateway["daily_query"]["kind"] == "parameter_optimization"
    assert gateway["daily_query"]["user_id"] == OWNER
    gateway["submit"].assert_not_called()


def test_parameter_space_endpoint_reports_declaration_and_limits(client, gateway, sources, compiled):
    response = client.get("/api/agent/v1/optimization/parameter-space?sourceId=11", headers=HEADERS)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert [item["name"] for item in data["parameters"]] == ["fast", "slow"]
    assert data["modes"] == ["sweep", "walkForward"]
    assert data["limits"] == LIMITS.public()
    assert data["strategySource"]["versionId"] == 101

    other = client.get("/api/agent/v1/optimization/parameter-space?sourceId=12", headers=HEADERS)
    assert other.status_code == 404


def test_runner_uses_pinned_version_and_reports_source(monkeypatch, sources):
    observations = _days(200)
    evaluator = FakeEvaluator(lambda params, *_: float(params["fast"]), observations)
    factory = Mock(return_value=evaluator)
    monkeypatch.setattr(optimizations, "_make_evaluator", factory)
    monkeypatch.setattr(optimizations, "optimization_limits", lambda: LIMITS)
    payload = {
        **DATES,
        "__user_id": OWNER,
        optimizations.MODE_KEY: "sweep",
        backtests.SOURCE_REF_KEY: {"sourceId": 11, "versionId": 101, "versionNo": 1, "name": "Mine"},
    }

    result = optimizations._run_optimization(payload)

    assert factory.call_args.kwargs["user_id"] == OWNER
    assert factory.call_args.kwargs["source_id"] == 11
    assert factory.call_args.kwargs["end_date"] == datetime(2024, 12, 31, 23, 59, 59)
    assert result["strategySource"] == {"sourceId": 11, "versionId": 101, "versionNo": 1}
    assert result["bestParams"]["fast"] == 20
    assert set(result) >= {"inSample", "outOfSample", "bestParamsOutOfSample", "history", "combinations"}


def test_runner_rejects_version_of_another_tenant(monkeypatch, sources):
    monkeypatch.setattr(optimizations, "_make_evaluator", Mock())
    payload = {
        **DATES,
        "__user_id": OWNER,
        optimizations.MODE_KEY: "sweep",
        backtests.SOURCE_REF_KEY: {"sourceId": 12, "versionId": 201, "versionNo": 1},
    }
    with pytest.raises(ValueError, match="sourceVersionNotFound"):
        optimizations._run_optimization(payload)


def test_celery_supports_optimization_jobs(monkeypatch):
    from app.tasks import agent_jobs as celery_agent_jobs

    calls = []
    monkeypatch.setattr(optimizations, "_run_optimization", lambda payload, on_progress: calls.append(payload) or {})
    assert celery_agent_jobs.supports_kind("parameter_optimization") is True
    celery_agent_jobs._execute("parameter_optimization", {"x": 1}, lambda _event: None)
    assert calls == [{"x": 1}]
