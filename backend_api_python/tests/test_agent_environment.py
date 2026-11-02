"""Agent environment readiness report (#41)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.routes.agent_v1 import health
from app.services.agent_environment import build_environment_report, worst_status
from app.services.backtest_limits import backtest_range_limit_summary, backtest_range_policy
from app.utils import agent_auth
from app.utils.agent_jobs import concurrent_job_limits

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _token(**overrides):
    token = {
        "id": 7, "user_id": 5, "name": "example-agent", "scopes": "R,W,B",
        "markets": "*", "instruments": "*", "paper_only": True,
        "rate_limit_per_min": 60, "status": "active", "expires_at": None,
    }
    token.update(overrides)
    return token


def _report(token=None, *, tenant_active=0, token_active=0, tenant_limit=4, token_limit=2):
    return build_environment_report(
        token=token or _token(), app_version="1.2.3",
        tenant_limit=tenant_limit, token_limit=token_limit,
        tenant_active=tenant_active, token_active=token_active, now=NOW,
    )


def _checks(report):
    return {item["id"]: item for item in report["checks"]}


def test_ready_token_passes_every_check():
    report = _report()
    checks = _checks(report)
    assert set(checks) == {"scopes", "paper_only", "backtest_quota", "backtest_range_limits", "token_expiry"}
    assert all(item["status"] == "pass" and item["remediation"] is None for item in checks.values())
    assert report["overall"] == "pass"
    assert report["app_version"] == "1.2.3"
    assert report["token"]["scopes"] == ["R", "W", "B"]
    assert report["token"]["paper_only"] is True
    assert report["backtest_quota"] == {
        "tenant_limit": 4, "tenant_active": 0, "token_limit": 2, "token_active": 0, "available": 2,
    }


def test_missing_backtest_scope_fails_with_reissue_hint():
    report = _report(_token(scopes="R"))
    checks = _checks(report)
    assert checks["scopes"]["status"] == "fail"
    assert "B scope" in checks["scopes"]["remediation"]
    assert "/me/tokens" in checks["scopes"]["remediation"]
    assert checks["backtest_quota"]["status"] == "warn"
    assert report["overall"] == "fail"


def test_missing_write_scope_warns():
    checks = _checks(_report(_token(scopes="R,B")))
    assert checks["scopes"]["status"] == "warn"
    assert "W scope" in checks["scopes"]["remediation"]


@pytest.mark.parametrize(("scopes", "paper_only", "status"), [
    ("R,W,B", True, "pass"),
    ("R,W,B,T", True, "pass"),
    ("R,W,B", False, "pass"),
    ("R,W,B,T", False, "warn"),
])
def test_paper_only_check(scopes, paper_only, status):
    check = _checks(_report(_token(scopes=scopes, paper_only=paper_only)))["paper_only"]
    assert check["status"] == status
    if status == "warn":
        assert "paper_only=true" in check["remediation"]


@pytest.mark.parametrize(("tenant_active", "token_active", "available", "status"), [
    (1, 1, 1, "pass"),
    (2, 2, 0, "warn"),
    (4, 0, 0, "warn"),
])
def test_backtest_quota_reports_free_slots(tenant_active, token_active, available, status):
    report = _report(tenant_active=tenant_active, token_active=token_active)
    assert report["backtest_quota"]["available"] == available
    check = _checks(report)["backtest_quota"]
    assert check["status"] == status
    if status == "warn":
        assert "cancel_job" in check["remediation"]


def test_unavailable_job_count_warns_instead_of_failing():
    report = _report(tenant_active=None, token_active=None)
    assert report["backtest_quota"]["available"] is None
    assert _checks(report)["backtest_quota"]["status"] == "warn"
    assert report["overall"] == "warn"


@pytest.mark.parametrize(("expires_at", "status"), [
    (None, "pass"),
    (NOW + timedelta(days=30), "pass"),
    (NOW + timedelta(days=2), "warn"),
    ((NOW + timedelta(days=2)).replace(tzinfo=None), "warn"),
])
def test_token_expiry(expires_at, status):
    report = _report(_token(expires_at=expires_at))
    assert _checks(report)["token_expiry"]["status"] == status
    if expires_at is not None:
        assert report["token"]["expires_at"].endswith("Z")


def test_range_limits_follow_backtest_policy_and_market_allowlist():
    summary = _report(_token(markets="USStock,Crypto"))["backtest_range_limits"]
    assert summary["warmup_included"] is True
    assert set(summary["markets"]) == {"USStock"}
    for timeframe, item in summary["default"].items():
        assert item["max_days"] == backtest_range_policy("Crypto", timeframe).max_days
    for timeframe, item in summary["markets"]["USStock"].items():
        assert item["max_days"] == backtest_range_policy("USStock", timeframe).max_days

    assert set(backtest_range_limit_summary(None)["markets"]) == {"USStock", "Forex", "TWStock"}
    assert set(backtest_range_limit_summary(["*"])["markets"]) == {"USStock", "Forex", "TWStock"}


def test_worst_status_treats_unknown_as_fail():
    assert worst_status([]) == "pass"
    assert worst_status(["pass", "warn"]) == "warn"
    assert worst_status(["warn", "bogus"]) == "fail"


def test_concurrent_job_limits_read_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_JOBS_PER_TENANT", "6")
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_JOBS_PER_TOKEN", "0")
    assert concurrent_job_limits() == (6, 1)


# --- gateway route ------------------------------------------------------------

RAW_TOKEN = "qd_agent_ENVTEST1"


@pytest.fixture
def agent_token(monkeypatch):
    token = _token(token_hash="hash-example", max_order_notional=1000)
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    yield token
    agent_auth._rate_state.clear()


def _call(client):
    return client.get("/api/agent/v1/environment", headers={"Authorization": f"Bearer {RAW_TOKEN}"})


def test_route_returns_report_without_secrets(client, agent_token, monkeypatch):
    seen = []

    def count(**kwargs):
        seen.append(kwargs)
        return 1

    monkeypatch.setattr(health, "count_active_jobs", count)
    response = _call(client)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["overall"] == "pass"
    assert data["backtest_quota"]["token_active"] == 1
    assert {"user_id": 5} in seen and {"user_id": 5, "agent_token_id": 7} in seen
    assert {item["id"] for item in data["checks"]} >= {"scopes", "paper_only", "backtest_quota"}

    body = response.get_data(as_text=True)
    assert RAW_TOKEN not in body
    assert "hash-example" not in body
    assert "token_hash" not in body
    assert "id" not in data["token"]
    assert "user_id" not in json.dumps(data)


def test_route_survives_job_count_failure(client, agent_token, monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(health, "count_active_jobs", broken)
    response = _call(client)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["backtest_quota"]["available"] is None
    assert "database unavailable" not in response.get_data(as_text=True)


def test_route_requires_token(client, agent_token):
    assert client.get("/api/agent/v1/environment").status_code == 401
