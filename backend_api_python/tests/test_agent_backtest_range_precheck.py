"""Submission-time range/warmup precheck and structured job failures (#24)."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import app.services.strategy_v2 as strategy_v2_pkg
from app.routes.agent_v1 import backtests, jobs as job_routes
from app.services.backtest_limits import BacktestRangeLimitError
from app.services.strategy_v2 import service as strategy_service
from app.services.strategy_v2.models import (
    InstrumentSpec, StrategyManifest, SubscriptionSpec, UniverseSpec,
)
from app.services.strategy_v2.service import precheck_backtest_range
from app.utils import agent_auth, agent_jobs


# Manifests are built directly so these tests do not depend on the compile
# sandbox (which cannot apply memory limits on macOS).
def _manifest(frequency: str = "15m", warmup: int = 0) -> StrategyManifest:
    instrument = InstrumentSpec(market="Crypto", symbol="BTC/USDT")
    return StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cta",
        universe=UniverseSpec(kind="static", instruments=(instrument,)),
        subscriptions=(SubscriptionSpec(instruments=(instrument,), frequency=frequency),),
        schedules=(),
        handlers=("handle_data",),
        warmup_bars=warmup,
    )


def _range_error(manifest: StrategyManifest, start: str, end: str) -> dict:
    with pytest.raises(BacktestRangeLimitError) as caught:
        precheck_backtest_range(
            manifest,
            start_date=datetime.strptime(start, "%Y-%m-%d"),
            end_date=datetime.strptime(end, "%Y-%m-%d"),
        )
    return caught.value.details


def test_precheck_rejects_15m_range_over_365_days_with_suggested_dates():
    details = _range_error(_manifest("15m"), "2023-01-01", "2024-12-31")

    assert details["error_type"] == "BACKTEST_RANGE_LIMIT"
    assert details["timeframe"] == "15m"
    assert details["max_days"] == 365
    assert details["warmup_days"] == 0
    assert details["fetch_days"] > 365
    assert details["recommendation_available"] is True
    assert details["recommended_start"] == "2024-01-02"
    assert details["recommended_end"] == "2023-12-31"


def test_precheck_rejects_when_warmup_alone_exceeds_limit():
    # 40,000 bars of 15m warmup need ~625 calendar days, above the 365-day cap.
    details = _range_error(_manifest("15m", warmup=40_000), "2024-06-01", "2024-06-30")

    assert details["max_days"] == 365
    assert details["warmup_bars"] == 40_000
    assert details["warmup_days"] > 365
    assert details["recommendation_available"] is False
    assert details["recommended_start"] is None
    assert details["recommended_end"] is None
    assert "warmup alone exceeds" in details["msg"]


def test_precheck_accepts_range_inside_limit():
    precheck_backtest_range(
        _manifest("15m", warmup=96),
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 6, 30),
    )


def test_precheck_uses_catalog_underlying_market_like_job_time_check(monkeypatch):
    # A tokenized US stock on a crypto venue is validated against USStock
    # limits (1D: 3650 days), not the default crypto limit (1D: 1095 days).
    monkeypatch.setattr(strategy_service, "get_catalog_product", lambda **_kw: {
        "instrument_id": "AAPLX",
        "product_type": "direct_equity",
        "api_family": "spot",
        "underlying_market": "USStock",
        "underlying_symbol": "AAPL",
    })
    instrument = InstrumentSpec(market="Crypto", symbol="AAPLX/USDT", exchange_id="bitget")
    manifest = StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cta",
        universe=UniverseSpec(kind="static", instruments=(instrument,)),
        subscriptions=(SubscriptionSpec(instruments=(instrument,), frequency="1d"),),
        schedules=(),
    )
    precheck_backtest_range(
        manifest,
        start_date=datetime(2020, 1, 1),
        end_date=datetime(2024, 12, 31),
    )


def test_precheck_skips_dynamic_universe_for_job_time_check():
    manifest = StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cross_sectional",
        universe=UniverseSpec(kind="dynamic", reference="top-crypto"),
        subscriptions=(SubscriptionSpec(universe_reference="top-crypto", frequency="1m"),),
        schedules=(),
    )
    precheck_backtest_range(
        manifest,
        start_date=datetime(2020, 1, 1),
        end_date=datetime(2024, 12, 31),
    )


def test_precheck_matches_job_time_check(monkeypatch):
    manifest = _manifest("15m", warmup=200)
    details = _range_error(manifest, "2023-06-01", "2024-06-30")
    monkeypatch.setattr(
        strategy_service, "compile_strategy_v2", lambda _code: SimpleNamespace(manifest=manifest),
    )

    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("market data must not be fetched")

    service = strategy_service.StrategyV2BacktestService(
        repository=object(),
        universe_service=object(),
        frame_fetcher=unexpected_fetch,
        snapshot_store=object(),
    )
    with pytest.raises(BacktestRangeLimitError) as caught:
        service.run(
            user_id=1,
            code="compiled-by-fixture",
            start_date=datetime(2023, 6, 1),
            end_date=datetime(2024, 6, 30, 23, 59, 59),
            initial_capital=10_000,
            persist=False,
        )
    assert caught.value.details == details


@pytest.fixture
def submission(monkeypatch):
    token = {"id": 7, "user_id": 5, "scopes": "R,B", "markets": "*", "instruments": "*",
             "status": "active", "rate_limit_per_min": 100, "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(agent_auth, "_reserve_idempotency", lambda *_: ("reserved", None))
    monkeypatch.setattr(agent_auth, "_complete_idempotency", lambda *_: None)
    monkeypatch.setattr(backtests, "count_active_jobs", lambda **kw: 0)

    @contextmanager
    def no_existing(_):
        yield None

    monkeypatch.setattr(backtests, "with_idempotency", no_existing)
    submit = Mock(return_value={"job_id": "job-1", "status": "queued"})
    monkeypatch.setattr(backtests, "submit_job", submit)
    yield submit
    agent_auth._rate_state.clear()


def _post(client, monkeypatch, manifest: StrategyManifest, start: str, end: str, **extra):
    monkeypatch.setattr(
        strategy_v2_pkg, "compile_strategy_v2", lambda _code: SimpleNamespace(manifest=manifest),
    )
    return client.post(
        "/api/agent/v1/backtest/run",
        json={"code": "compiled-by-fixture", "startDate": start, "endDate": end, **extra},
        headers={"Authorization": "Bearer qd_agent_RANGETEST12345", "Idempotency-Key": "range-test"},
    )


@pytest.mark.parametrize(
    ("warmup", "start", "end", "recommendation"),
    [
        (0, "2023-01-01", "2024-12-31", True),
        (40_000, "2024-06-01", "2024-06-30", False),
    ],
    ids=["15m-over-365-days", "warmup-alone-over-limit"],
)
def test_submit_returns_400_with_range_details_and_creates_no_job(
    client, submission, monkeypatch, warmup, start, end, recommendation,
):
    response = _post(client, monkeypatch, _manifest("15m", warmup=warmup), start, end)

    assert response.status_code == 400
    body = response.get_json()
    assert body["message"] == "strategyV2.backtestRangeLimit"
    assert body["retriable"] is False
    details = body["details"]
    assert details["error_type"] == "BACKTEST_RANGE_LIMIT"
    assert details["max_days"] == 365
    assert details["warmup_bars"] == warmup
    assert "warmup_days" in details
    assert details["recommendation_available"] is recommendation
    assert (details["recommended_start"] is not None) is recommendation
    assert (details["recommended_end"] is not None) is recommendation
    submission.assert_not_called()


def test_submit_inside_limit_is_queued(client, submission, monkeypatch):
    response = _post(client, monkeypatch, _manifest("15m", warmup=96), "2024-01-01", "2024-06-30")

    assert response.status_code == 202
    submission.assert_called_once()


def test_submit_forwards_historical_funding_mode(client, submission, monkeypatch):
    response = _post(
        client, monkeypatch, _manifest("15m", warmup=96), "2024-01-01", "2024-06-30",
        fundingMode="historical_rates",
    )

    assert response.status_code == 202
    assert submission.call_args.kwargs["request_payload"]["fundingMode"] == "historical_rates"


def test_submit_rejects_unknown_funding_mode(client, submission, monkeypatch):
    response = _post(
        client, monkeypatch, _manifest("15m", warmup=96), "2024-01-01", "2024-06-30",
        fundingMode="fixed",
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["message"] == "strategyV2.fundingModeUnsupported:fixed"
    assert body["details"]["allowed"] == ["not_modeled", "historical_rates"]
    submission.assert_not_called()


def test_submit_forwards_tw_futures_session(client, submission, monkeypatch):
    response = _post(
        client, monkeypatch, _manifest("15m", warmup=96), "2024-01-01", "2024-06-30",
        session="regular",
    )

    assert response.status_code == 202
    assert submission.call_args.kwargs["request_payload"]["session"] == "regular"


def test_submit_rejects_unknown_tw_futures_session(client, submission, monkeypatch):
    response = _post(
        client, monkeypatch, _manifest("15m", warmup=96), "2024-01-01", "2024-06-30",
        session="night",
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["message"] == "strategyV2.twFuturesSessionUnsupported:night"
    assert body["details"]["allowed"] == ["all", "regular"]
    submission.assert_not_called()


def _range_exception() -> BacktestRangeLimitError:
    return BacktestRangeLimitError({
        "error_type": "BACKTEST_RANGE_LIMIT",
        "msg": "Backtest range exceeds limit",
        "max_days": 365,
        "warmup_days": 625,
        "recommendation_available": False,
        "recommended_start": None,
        "recommended_end": None,
    })


def _expected_failure(exc: BacktestRangeLimitError) -> dict:
    return {
        "code": "strategyV2.backtestRangeLimit",
        "message": "Backtest range exceeds limit",
        "details": exc.details,
    }


def test_thread_runner_persists_structured_failure(monkeypatch):
    exc = _range_exception()

    @contextmanager
    def fake_db():
        yield MagicMock()

    class InlineExecutor:
        def submit(self, fn):
            fn()

    monkeypatch.delenv("CELERY_TASKS_ENABLED", raising=False)
    monkeypatch.setattr(agent_jobs, "get_db_connection", fake_db)
    monkeypatch.setattr(agent_jobs, "_get_executor", lambda: InlineExecutor())
    monkeypatch.setattr(agent_jobs, "_set_status", lambda *a, **kw: True)
    monkeypatch.setattr(agent_jobs, "get_job_for_worker", lambda _: {"status": "running"})
    published = Mock()
    monkeypatch.setattr(agent_jobs, "_publish_progress", published)
    failure = Mock(return_value=True)
    monkeypatch.setattr(agent_jobs, "_set_failure", failure)

    def runner(_payload):
        raise exc

    agent_jobs.submit_job(
        user_id=5, agent_token_id=None, kind="range_test",
        request_payload={}, runner=runner,
    )

    stored = failure.call_args.args[1]
    assert json.loads(stored) == _expected_failure(exc)
    assert agent_jobs.decode_job_error(stored) == _expected_failure(exc)
    assert published.call_args.args[1]["failure"] == exc.details


def test_celery_runner_persists_same_structured_failure(monkeypatch):
    from app.tasks import agent_jobs as tasks

    exc = _range_exception()
    monkeypatch.setattr(agent_jobs, "get_job_for_worker", lambda _: {
        "status": "queued", "kind": "backtest", "request": {}})
    monkeypatch.setattr(agent_jobs, "_set_status", lambda *a, **kw: True)
    monkeypatch.setattr(agent_jobs, "_publish_progress", Mock())
    failure = Mock(return_value=True)
    monkeypatch.setattr(agent_jobs, "_set_failure", failure)
    monkeypatch.setattr(tasks, "_execute", Mock(side_effect=exc))

    with pytest.raises(BacktestRangeLimitError):
        tasks.execute_agent_job.run("celery-job")

    stored = failure.call_args.args[1]
    assert agent_jobs.decode_job_error(stored) == _expected_failure(exc)


def test_plain_failures_keep_string_error():
    assert agent_jobs.job_failure_text(ValueError("boom")) == "boom"
    assert agent_jobs.job_failure_text(ValueError("boom"), "tb") == "boom\ntb"
    assert agent_jobs.decode_job_error("boom\nTraceback") == "boom\nTraceback"
    assert agent_jobs.decode_job_error('{"not": "structured"}') == '{"not": "structured"}'
    assert agent_jobs.decode_job_error(None) is None


def test_agent_job_endpoint_returns_structured_error(client, monkeypatch):
    exc = _range_exception()
    token = {"id": 7, "user_id": 5, "scopes": "R", "markets": "*", "instruments": "*",
             "status": "active", "rate_limit_per_min": 100, "expires_at": None}
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(job_routes, "get_job", lambda job_id, user_id: {
        "job_id": job_id, "status": "failed", "kind": "backtest",
        "error": agent_jobs.job_failure_text(exc),
    })

    response = client.get(
        "/api/agent/v1/jobs/job-1",
        headers={"Authorization": "Bearer qd_agent_RANGETEST12345"},
    )
    agent_auth._rate_state.clear()

    assert response.status_code == 200
    assert response.get_json()["data"]["error"] == _expected_failure(exc)


def test_human_backtest_job_keeps_string_error_and_adds_failure():
    from app.routes.backtest_center import _public_backtest_job

    exc = _range_exception()
    public = _public_backtest_job({
        "job_id": "job-1", "status": "failed", "request": {},
        "error": agent_jobs.job_failure_text(exc),
    })

    assert public["error"] == "strategyV2.backtestRangeLimit"
    assert public["failure"] == exc.details
