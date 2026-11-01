"""Agent backtests from a saved strategy source and version (#27)."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.services.strategy_v2 as strategy_v2_pkg
from app.routes.agent_v1 import backtests
from app.services.strategy_v2.models import (
    InstrumentSpec, StrategyManifest, SubscriptionSpec, UniverseSpec,
)
from app.utils import agent_auth

OWNER = 5
OTHER_TENANT = 9
HEADERS = {"Authorization": "Bearer qd_agent_SOURCETEST1234", "Idempotency-Key": "source-test"}
DATES = {"startDate": "2024-01-01", "endDate": "2024-03-31"}


def _manifest() -> StrategyManifest:
    instrument = InstrumentSpec(market="Crypto", symbol="BTC/USDT")
    return StrategyManifest(
        api_version=2,
        code_hash="test",
        strategy_type="cta",
        universe=UniverseSpec(kind="static", instruments=(instrument,)),
        subscriptions=(SubscriptionSpec(instruments=(instrument,), frequency="1h"),),
        schedules=(),
        handlers=("handle_data",),
    )


class FakeSources:
    """In-memory, tenant-scoped stand-in for ScriptSourceService."""

    def __init__(self):
        self.sources = {
            11: {"id": 11, "user_id": OWNER, "name": "Mine", "metadata": {}},
            12: {"id": 12, "user_id": OTHER_TENANT, "name": "Theirs", "metadata": {}},
            13: {"id": 13, "user_id": OWNER, "name": "Bought", "metadata": {"code_hidden": True}},
            14: {"id": 14, "user_id": OWNER, "name": "Legacy", "metadata": {}},
        }
        self.versions = [
            {"id": 101, "source_id": 11, "user_id": OWNER, "version_no": 1, "code": "code-v1"},
            {"id": 102, "source_id": 11, "user_id": OWNER, "version_no": 2, "code": "code-v2"},
            {"id": 201, "source_id": 12, "user_id": OTHER_TENANT, "version_no": 1, "code": "other"},
            {"id": 301, "source_id": 13, "user_id": OWNER, "version_no": 1, "code": "hidden"},
        ]

    def get_source(self, source_id, user_id=None):
        row = self.sources.get(int(source_id))
        if not row or (user_id is not None and row["user_id"] != int(user_id)):
            return None
        return dict(row)

    def _owned(self, source_id, user_id):
        return [
            v for v in self.versions
            if v["source_id"] == int(source_id) and v["user_id"] == int(user_id)
        ]

    def get_latest_version(self, source_id, user_id):
        rows = self._owned(source_id, user_id)
        return dict(max(rows, key=lambda v: v["version_no"])) if rows else None

    def get_version_by_number(self, source_id, version_no, user_id):
        if not self.get_source(source_id, user_id=user_id):
            return None
        rows = [v for v in self._owned(source_id, user_id) if v["version_no"] == int(version_no)]
        return dict(rows[0]) if rows else None

    def get_version(self, version_id, user_id):
        rows = [v for v in self.versions if v["id"] == int(version_id) and v["user_id"] == int(user_id)]
        return dict(rows[0]) if rows else None


@pytest.fixture
def sources(monkeypatch):
    fake = FakeSources()
    monkeypatch.setattr(backtests, "get_script_source_service", lambda: fake)
    return fake


@pytest.fixture
def compiled(monkeypatch):
    compile_mock = Mock(return_value=SimpleNamespace(manifest=_manifest()))
    monkeypatch.setattr(strategy_v2_pkg, "compile_strategy_v2", compile_mock)
    return compile_mock


@pytest.fixture
def submission(monkeypatch):
    token = {"id": 7, "user_id": OWNER, "scopes": "R,B", "markets": "*", "instruments": "*",
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


def _post(client, body):
    return client.post("/api/agent/v1/backtest/run", json={**DATES, **body}, headers=HEADERS)


def test_source_id_without_version_uses_latest_version(client, submission, sources, compiled):
    response = _post(client, {"sourceId": 11})

    assert response.status_code == 202
    assert response.get_json()["data"]["strategySource"] == {
        "sourceId": 11, "versionId": 102, "versionNo": 2,
    }
    compiled.assert_called_once_with("code-v2")
    payload = submission.call_args.kwargs["request_payload"]
    assert payload[backtests.SOURCE_REF_KEY] == {
        "sourceId": 11, "versionId": 102, "versionNo": 2, "name": "Mine",
    }
    # The job pins the version instead of copying the code body.
    assert "code" not in payload


def test_source_version_selects_that_snapshot(client, submission, sources, compiled):
    response = _post(client, {"sourceId": 11, "sourceVersion": 1})

    assert response.status_code == 202
    assert response.get_json()["data"]["strategySource"]["versionNo"] == 1
    compiled.assert_called_once_with("code-v1")


@pytest.mark.parametrize("body", [
    {"sourceId": 12},
    {"sourceId": 12, "sourceVersion": 1},
    {"sourceId": 999},
], ids=["other-tenant-latest", "other-tenant-version", "missing"])
def test_other_tenant_or_missing_source_is_404(client, submission, sources, compiled, body):
    response = _post(client, body)

    assert response.status_code == 404
    assert response.get_json()["message"] == "Strategy source not found"
    compiled.assert_not_called()
    submission.assert_not_called()


def test_unknown_version_is_404(client, submission, sources, compiled):
    response = _post(client, {"sourceId": 11, "sourceVersion": 9})

    assert response.status_code == 404
    assert response.get_json()["message"] == "Strategy source version not found"
    submission.assert_not_called()


def test_hidden_source_is_403(client, submission, sources, compiled):
    response = _post(client, {"sourceId": 13})

    assert response.status_code == 403
    submission.assert_not_called()


def test_source_without_versions_is_409(client, submission, sources, compiled):
    response = _post(client, {"sourceId": 14})

    assert response.status_code == 409
    submission.assert_not_called()


@pytest.mark.parametrize(("body", "message"), [
    ({"code": "inline", "sourceId": 11}, "Provide either code or sourceId, not both"),
    ({}, "code or sourceId is required"),
    ({"code": "inline", "sourceVersion": 1}, "sourceVersion requires sourceId"),
    ({"sourceId": "11"}, "sourceId must be a positive integer"),
    ({"sourceId": True}, "sourceId must be a positive integer"),
    ({"sourceId": 0}, "sourceId must be a positive integer"),
    ({"sourceId": 11, "sourceVersion": -1}, "sourceVersion must be a positive integer"),
], ids=["both", "neither", "version-without-source", "string-id", "bool-id", "zero-id", "negative-version"])
def test_code_and_source_id_are_mutually_exclusive(client, submission, sources, compiled, body, message):
    response = _post(client, body)

    assert response.status_code == 400
    assert response.get_json()["message"] == message
    submission.assert_not_called()


def test_client_cannot_inject_server_source_reference(client, submission, sources, compiled):
    response = _post(client, {"code": "inline", backtests.SOURCE_REF_KEY: {"sourceId": 12}})

    assert response.status_code == 400
    submission.assert_not_called()


def test_inline_code_still_supported(client, submission, sources, compiled):
    response = _post(client, {"code": "inline"})

    assert response.status_code == 202
    assert "strategySource" not in response.get_json()["data"]
    assert backtests.SOURCE_REF_KEY not in submission.call_args.kwargs["request_payload"]


def _job_payload(ref: dict, user_id: int = OWNER) -> dict:
    return {**DATES, "__user_id": user_id, backtests.SOURCE_REF_KEY: ref}


def test_runner_runs_pinned_version_and_records_it(monkeypatch, sources):
    calls = []

    def fake_run(payload, on_progress=None, *, service=None):
        calls.append(payload)
        return {"totalReturn": 1.5, "runId": 77}

    monkeypatch.setattr(backtests, "run_backtest_job", fake_run)
    ref = {"sourceId": 11, "versionId": 101, "versionNo": 1, "name": "Mine"}

    result = backtests._run_backtest(_job_payload(ref))

    assert calls[0]["code"] == "code-v1"
    assert calls[0]["__source_id"] == 11
    assert calls[0]["__strategy_name"] == "Mine"
    assert result["strategySource"] == {"sourceId": 11, "versionId": 101, "versionNo": 1}
    assert result["runId"] == 77


@pytest.mark.parametrize("ref", [
    {"sourceId": 12, "versionId": 201, "versionNo": 1},
    {"sourceId": 12, "versionId": 101, "versionNo": 1},
], ids=["other-tenant-version", "version-of-different-source"])
def test_runner_rejects_versions_outside_tenant_or_source(monkeypatch, sources, ref):
    monkeypatch.setattr(backtests, "run_backtest_job", Mock())

    with pytest.raises(ValueError, match="sourceVersionNotFound"):
        backtests._run_backtest(_job_payload(ref))
