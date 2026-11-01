"""Agent Gateway backtest history (list + get by runId) tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.routes.agent_v1 import backtest_runs
from app.services.strategy_v2.storage import StrategyBacktestRepository
from app.utils import agent_auth


TENANT_USER_ID = 7


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    agent_auth._rate_state.clear()
    yield
    agent_auth._rate_state.clear()


def _authorize(monkeypatch, scopes: str = "R") -> None:
    token = {
        "id": 901,
        "user_id": TENANT_USER_ID,
        "name": "history-agent",
        "scopes": scopes,
        "markets": "*",
        "instruments": "*",
        "paper_only": True,
        "rate_limit_per_min": 100,
        "status": "active",
        "expires_at": None,
    }
    agent_auth._schema_ready = True
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda raw: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)


HEADERS = {"Authorization": "Bearer qd_agent_BACKTESTHISTORY12345"}


def _row(run_id: int, **overrides) -> dict:
    row = {
        "id": run_id,
        "user_id": TENANT_USER_ID,
        "strategy_id": None,
        "source_id": 12,
        "strategy_name": "SPY trend",
        "market": "USStock",
        "symbol": "SPY",
        "market_type": "spot",
        "timeframe": "1D",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 10000,
        "commission": 0.001,
        "slippage": 0.0005,
        "leverage": 1,
        "params": {"fast": 10},
        "manifest": {"strategyType": "single"},
        "engine_version": "strategy-api-v2",
        "code_hash": "abc123",
        "status": "success",
        "created_at": datetime(2026, 10, 30, 12, 0, 0),
        "total_return": 12.5,
        "win_rate": 55.0,
        "total_trades": 20,
        "total_executions": 40,
        "result_status": "complete",
        "data_kind": "market",
        "benchmark_total_return": 8.0,
        "max_drawdown": -6.4,
        "sharpe_ratio": 1.1,
        "total_commission": 18.75,
    }
    row.update(overrides)
    return row


class _Repository:
    """Tenant-aware fake mirroring StrategyBacktestRepository's user filter."""

    def __init__(self, rows):
        self.rows = rows
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def list_runs(self, **kwargs):
        self.list_calls.append(kwargs)
        owned = [row for row in self.rows if row["user_id"] == kwargs["user_id"]]
        start = kwargs["offset"]
        return owned[start:start + kwargs["limit"]]

    def get_run(self, *, user_id, run_id):
        self.get_calls.append({"user_id": user_id, "run_id": run_id})
        for row in self.rows:
            if row["id"] == run_id and row["user_id"] == user_id:
                return dict(row)
        return None


@pytest.fixture
def repository(monkeypatch):
    repo = _Repository([
        _row(43),
        _row(42, total_commission=None),
        _row(41),
        _row(99, user_id=8, strategy_name="other tenant"),
    ])
    monkeypatch.setattr(backtest_runs, "get_backtest_run_repository", lambda: repo)
    return repo


def test_list_returns_tenant_runs_with_summary_metrics(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs?limit=2", headers=HEADERS)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert [item["runId"] for item in data["items"]] == [43, 42]
    assert data["pagination"] == {"limit": 2, "offset": 0, "hasMore": True, "nextOffset": 2}
    first = data["items"][0]
    assert first["metrics"] == {
        "totalReturn": 12.5,
        "maxDrawdown": -6.4,
        "totalTrades": 20,
        "winRate": 55.0,
        "totalFees": 18.75,
        "sharpeRatio": 1.1,
        "totalExecutions": 40,
        "benchmarkTotalReturn": 8.0,
    }
    assert first["sourceId"] == 12
    assert first["codeHash"] == "abc123"
    assert first["createdAt"] == "2026-10-30T12:00:00"
    assert "result" not in first and "manifest" not in first
    # Legacy runs without a stored fee total report null rather than zero.
    assert data["items"][1]["metrics"]["totalFees"] is None

    call = repository.list_calls[0]
    assert call["user_id"] == TENANT_USER_ID
    assert call["limit"] == 3  # one extra row decides hasMore


def test_list_last_page_and_tenant_isolation(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs?limit=2&offset=2", headers=HEADERS)

    data = response.get_json()["data"]
    assert [item["runId"] for item in data["items"]] == [41]
    assert data["pagination"]["hasMore"] is False
    assert data["pagination"]["nextOffset"] is None
    assert all(item["strategyName"] != "other tenant" for item in data["items"])


def test_list_forwards_filters_and_parses_time_bounds(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get(
        "/api/agent/v1/backtest/runs",
        query_string={
            "sourceId": "12",
            "strategyId": "5",
            "symbol": " SPY ",
            "market": "USStock",
            "timeframe": "1D",
            "createdFrom": "2026-10-01",
            "createdTo": "2026-10-31",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    call = repository.list_calls[0]
    assert call["source_id"] == 12
    assert call["strategy_id"] == 5
    assert call["symbol"] == "SPY"
    assert call["market"] == "USStock"
    assert call["timeframe"] == "1D"
    assert call["created_from"] == datetime(2026, 10, 1, 0, 0, 0)
    assert call["created_to"] == datetime(2026, 10, 31, 23, 59, 59, 999999)
    assert call["limit"] == backtest_runs.LIST_LIMIT_DEFAULT + 1


def test_list_accepts_iso_datetime_bounds(client, monkeypatch, repository):
    _authorize(monkeypatch)

    client.get(
        "/api/agent/v1/backtest/runs",
        query_string={"createdFrom": "2026-10-01T08:30:00Z"},
        headers=HEADERS,
    )

    assert repository.list_calls[0]["created_from"] == datetime(2026, 10, 1, 8, 30, 0)
    assert repository.list_calls[0]["created_to"] is None


@pytest.mark.parametrize("query", [
    {"sourceId": "abc"},
    {"strategyId": "0"},
    {"createdFrom": "yesterday"},
    {"createdFrom": "2026-10-31", "createdTo": "2026-10-01"},
])
def test_list_rejects_invalid_filters(client, monkeypatch, repository, query):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs", query_string=query, headers=HEADERS)

    assert response.status_code == 400
    assert repository.list_calls == []


def test_list_clamps_limit(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs?limit=5000", headers=HEADERS)

    assert response.get_json()["data"]["pagination"]["limit"] == backtest_runs.LIST_LIMIT_MAX


def test_get_returns_full_result_and_fee_fallback(client, monkeypatch, repository):
    _authorize(monkeypatch)
    repository.rows[1]["result"] = {"totalCommission": 9.5, "equityCurve": [{"time": "t", "value": 1}]}

    response = client.get("/api/agent/v1/backtest/runs/42", headers=HEADERS)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["runId"] == 42
    assert data["manifest"] == {"strategyType": "single"}
    assert data["result"]["equityCurve"] == [{"time": "t", "value": 1}]
    assert data["metrics"]["totalFees"] == 9.5
    assert repository.get_calls == [{"user_id": TENANT_USER_ID, "run_id": 42}]


def test_get_other_tenant_run_is_not_found(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs/99", headers=HEADERS)

    assert response.status_code == 404
    assert response.get_json()["data"] is None


def test_history_requires_read_scope(client, monkeypatch, repository):
    _authorize(monkeypatch, scopes="B")

    listed = client.get("/api/agent/v1/backtest/runs", headers=HEADERS)
    fetched = client.get("/api/agent/v1/backtest/runs/41", headers=HEADERS)

    assert listed.status_code == 403
    assert fetched.status_code == 403
    assert repository.list_calls == [] and repository.get_calls == []


class _Cursor:
    def __init__(self):
        self.query = ""
        self.params = ()

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        return []

    def close(self):
        pass


class _Connection:
    def __init__(self):
        self.cur = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cur


def test_repository_list_filters_by_tenant_and_creation_time(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(
        "app.services.strategy_v2.storage.get_db_connection",
        lambda: connection,
    )
    start = datetime(2026, 10, 1)
    end = datetime(2026, 10, 31, 23, 59, 59)

    StrategyBacktestRepository().list_runs(
        user_id=7,
        limit=21,
        offset=40,
        strategy_id=5,
        created_from=start,
        created_to=end,
    )

    query = connection.cur.query
    assert "user_id = ?" in query
    assert "created_at >= ?" in query and "created_at <= ?" in query
    assert "total_commission" in query
    assert "result_json" not in query.lower()
    assert connection.cur.params == (7, 5, start, end, 21, 40)


class _InsertCursor(_Cursor):
    lastrowid = 0

    def executemany(self, query, rows):
        pass


class _InsertConnection(_Connection):
    def __init__(self):
        self.cur = _InsertCursor()

    def commit(self):
        pass


def test_persist_run_stores_total_commission_summary(monkeypatch):
    connection = _InsertConnection()
    monkeypatch.setattr(
        "app.services.strategy_v2.storage.get_db_connection",
        lambda: connection,
    )

    StrategyBacktestRepository().persist_run(
        user_id=7, strategy_id=None, strategy_name="SPY trend", source_id=12,
        market="USStock", symbol="SPY", timeframe="1D",
        start_date="2024-01-01", end_date="2024-12-31",
        initial_capital=10000, commission=0.001, slippage=0.0, leverage=1,
        manifest={}, params={}, result={"totalReturn": 4.0, "totalCommission": 12.25},
        code="def initialize(context):\n    pass\n",
    )

    query = connection.cur.query
    columns = query.split("(", 1)[1].split(")", 1)[0]
    assert "total_commission" in columns
    assert query.count("?") == len(connection.cur.params)
    assert connection.cur.params[-1] == 12.25


def test_hydrate_exposes_total_commission_in_result():
    hydrated = StrategyBacktestRepository._hydrate(
        {"id": 1, "total_commission": None, "result_json": '{"totalCommission": 3.5}'},
        include_result=True,
    )

    assert hydrated["total_commission"] == 3.5
    assert hydrated["result"]["totalCommission"] == 3.5
