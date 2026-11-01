"""Markdown backtest report: snapshot, consistency, legacy runs and endpoints."""
from __future__ import annotations

import copy
import inspect
from pathlib import Path

import pytest
from flask import g

from app.routes import backtest_center
from app.routes.agent_v1 import backtest_runs
from app.services.strategy_v2.backtest_report import (
    REPORT_LANGUAGES,
    render_backtest_report,
    resolve_report_language,
)
from app.services.strategy_v2.trade_diagnostics import (
    summarize_trade_diagnostics,
    trade_diagnostics,
)
from app.utils import agent_auth

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "backtest_report"
TENANT_USER_ID = 7
HEADERS = {"Authorization": "Bearer qd_agent_BACKTESTREPORT12345"}

# side, entry, qty, high, low, stop, profit, commission, entry/exit slippage,
# entry time, exit time, holding bars
_TRADE_SPECS = (
    ("long", 40000.0, 0.1, 42500.0, 39600.0, 0.02, 180.0, 8.2, 2.0, 2.1,
     "2024-01-03T00:00:00", "2024-01-03T00:45:00", 3),
    ("long", 41000.0, 0.1, 41200.0, 40100.0, 0.02, -95.0, 8.1, 2.0, 2.0,
     "2024-02-10T00:00:00", "2024-02-11T06:00:00", 30),
    ("short", 45000.0, 0.1, 45300.0, 42000.0, 0.0, 260.0, 8.7, 2.2, 2.1,
     "2024-03-01T00:00:00", "2024-03-05T12:00:00", 108),
    ("long", 43000.0, 0.1, 43100.0, 40800.0, 0.025, -150.0, 8.4, 2.1, 2.0,
     "2024-04-01T00:00:00", "2024-04-12T00:00:00", 264),
)


def _trades() -> list[dict]:
    trades = []
    for side, entry, qty, high, low, stop, profit, fee, e_slip, x_slip, e_time, x_time, bars in _TRADE_SPECS:
        trade = {
            "symbol": "BTC/USDT",
            "side": side,
            "entry_time": e_time,
            "exit_time": x_time,
            "entry_price": entry,
            "quantity": qty,
            "profit": profit,
            "commission": fee,
            "close_reason": "signal",
        }
        trade.update(trade_diagnostics(
            side=side, entry_price=entry, quantity=qty, highest_price=high, lowest_price=low,
            stop_loss_pct=stop, profit=profit, commission=fee, entry_slippage=e_slip,
            exit_slippage=x_slip, entry_time=e_time, exit_time=x_time, holding_bars=bars,
        ))
        trades.append(trade)
    return trades


def _run(**overrides) -> dict:
    """A fixed persisted run as returned by StrategyBacktestRepository.get_run."""
    trades = _trades()
    result = {
        "initialCapital": 10000.0,
        "finalEquity": 10195.0,
        "totalReturn": 1.95,
        "annualizedReturn": 6.21,
        "annualizedReturnAvailable": True,
        "annualizedReturnCapped": False,
        "maxDrawdown": -3.42,
        "sharpeRatio": 0.84,
        "sortinoRatio": 1.17,
        "calmarRatio": 1.82,
        "benchmark": {"market": "Crypto", "symbol": "BTC/USDT", "market_type": "spot"},
        "benchmarkStatus": "partial",
        "benchmarkTotalReturn": 4.5,
        "benchmarkCoverageRatio": 0.982,
        "excessReturn": -2.55,
        "totalTrades": 4,
        "totalExecutions": 8,
        "winRate": 50.0,
        "profitFactor": 1.8,
        "totalCommission": 33.4,
        **summarize_trade_diagnostics(trades),
        "closedTrades": trades,
        "resultStatus": "completed_trades",
        "liquidated": False,
        "dataProvenance": {"kind": "market", "source": "exchange"},
        "diagnostics": {"symbolsSkipped": [], "symbolsRequested": 1, "symbolsUsed": 1},
        "engine": {"version": "strategy-v2-engine-test"},
        "strategySource": {"sourceId": 12, "versionId": 88, "versionNo": 3},
        "executionAssumptions": {
            "fillRule": "scheduled_current_open_or_signal_next_open",
            "protectionRule": "gap_open_then_intrabar_trigger",
            "intrabarMode": "conservative",
            "barClosePolicy": "closed_bars_only",
            "initialCapital": 10000.0,
            "startDate": "2024-01-01",
            "endDate": "2024-06-30",
            "leverageEnabled": False,
            "leverage": 1.0,
            "commission": 0.001,
            "slippage": 0.0005,
            "fundingMode": "not_modeled",
        },
    }
    run = {
        "id": 41,
        "user_id": TENANT_USER_ID,
        "strategy_id": None,
        "source_id": 12,
        "strategy_name": "BTC trend | test",
        "market": "Crypto",
        "symbol": "BTC/USDT",
        "market_type": "swap",
        "timeframe": "4H",
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "initial_capital": 10000,
        "commission": 0.001,
        "slippage": 0.0005,
        "leverage": 1,
        "params": {"fast": 10, "slow": 30},
        "engine_version": "strategy-api-v2",
        "code_hash": "abc123",
        "status": "success",
        "created_at": "2026-10-30T12:00:00",
        "total_return": 1.95,
        "win_rate": 50.0,
        "total_trades": 4,
        "total_executions": 8,
        "result_status": "completed_trades",
        "data_kind": "market",
        "benchmark_total_return": 4.5,
        "max_drawdown": -3.42,
        "sharpe_ratio": 0.84,
        "total_commission": 33.4,
        "result": result,
    }
    run.update(overrides)
    return run


def _legacy_run() -> dict:
    """A run persisted before trade diagnostics, assumptions and ratios existed."""
    return {
        "id": 7,
        "user_id": TENANT_USER_ID,
        "source_id": None,
        "strategy_name": "Old SPY run",
        "market": "USStock",
        "symbol": "SPY",
        "market_type": "",
        "timeframe": "1D",
        "start_date": "2022-01-01",
        "end_date": "2022-12-31",
        "initial_capital": 10000,
        "commission": 0.001,
        "slippage": 0.0,
        "leverage": 1,
        "params": {},
        "engine_version": "",
        "code_hash": "",
        "created_at": "2023-01-05T09:00:00",
        "total_return": -4.2,
        "win_rate": 40.0,
        "total_trades": 5,
        "max_drawdown": -9.1,
        "sharpe_ratio": None,
        "total_commission": None,
        "result_status": "unknown",
        "data_kind": "unknown",
        "result": {
            "totalReturn": -4.2,
            "winRate": 40.0,
            "totalTrades": 5,
            "closedTrades": [{"profit": 10.0}, {"profit": -20.0}],
        },
    }


def _assert_snapshot(name: str, content: str) -> None:
    path = SNAPSHOT_DIR / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        pytest.fail(f"snapshot {name} was missing and has been written; review and re-run")
    assert content == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("language", REPORT_LANGUAGES)
def test_report_matches_snapshot(language):
    _assert_snapshot(f"run-41.{language}.md", render_backtest_report(_run(), language=language))


def test_legacy_run_report_matches_snapshot():
    _assert_snapshot("legacy-run-7.en-US.md", render_backtest_report(_legacy_run(), language="en-US"))


def test_report_numbers_come_from_result_json():
    run = _run()
    result = run["result"]
    report = render_backtest_report(run)

    def row(label: str, value: str) -> None:
        assert f"| {label} | {value} |" in report, (label, value)

    row("Initial capital", f"{result['initialCapital']:,.2f}")
    row("Final equity", f"{result['finalEquity']:,.2f}")
    row("Total return", f"{result['totalReturn']:.2f}%")
    row("Buy-and-hold return (BTC/USDT)", f"{result['benchmarkTotalReturn']:.2f}%")
    row("Max drawdown", f"{result['maxDrawdown']:.2f}%")
    row("Sharpe ratio", f"{result['sharpeRatio']:.2f}")
    row("Sortino ratio", f"{result['sortinoRatio']:.2f}")
    row("Calmar ratio", f"{result['calmarRatio']:.2f}")
    row("Commission", f"{result['totalTradeCommission']:,.2f}")
    row("Slippage", f"{result['totalSlippageCost']:,.2f}")
    row("Total trading cost", f"{result['totalTradingCost']:,.2f}")
    row("Cost / gross profit", f"{result['costToGrossProfitPct']:.2f}%")
    row("Cost / gross loss", f"{result['costToGrossLossPct']:.2f}%")
    row("Closed trades", str(result["totalTrades"]))
    row("Win rate", f"{result['winRate']:.2f}%")
    row("Expectancy per trade before costs", f"{result['expectancyBeforeCosts']:,.2f}")
    row("Expectancy per trade after costs", f"{result['expectancyAfterCosts']:,.2f}")
    row("Average MAE", f"{result['averageMaePct']:.2f}%")
    row("Average MFE", f"{result['averageMfePct']:.2f}%")
    row("Funding", "Not modeled (not included)")
    row("Source ID", "12")
    row("Source version", "3")
    assert f"{result['averageR']:.2f} ({result['rMultipleTradeCount']} trades with a stop loss)" in report


def test_unavailable_benchmark_is_not_reported_as_zero_return():
    run = _run()
    run["result"].update(
        benchmarkStatus="unavailable",
        benchmarkError="strategyV2.benchmarkDataUnavailable",
        benchmarkTotalReturn=0.0,
    )

    report = render_backtest_report(run)

    assert "| Buy-and-hold return (BTC/USDT) | No data |" in report
    assert "| Excess return vs buy-and-hold | No data |" in report
    assert "`strategyV2.benchmarkDataUnavailable`" in report


def test_legacy_run_marks_missing_sections_as_no_data():
    report = render_backtest_report(_legacy_run(), language="zh-TW")

    assert "## 交易診斷" in report
    assert "### 持倉時間分布\n\n無資料" in report
    assert "### MAE／MFE\n\n無資料" in report
    assert "| 滑點 | 無資料 |" in report
    assert "| 勝率 | 40.00% |" in report


def test_report_tolerates_empty_and_malformed_results():
    for result in ({}, None, {"closedTrades": "bad", "executionAssumptions": "bad", "benchmark": 3}):
        report = render_backtest_report({"id": 5, "result": result})
        assert report.startswith("# Backtest Report: #5")
        assert "No data" in report


def test_sampled_history_is_disclosed():
    run = _run()
    run["result"]["totalTrades"] = 10

    report = render_backtest_report(run)

    assert "Based on 4 of 10 closed trades kept in the stored result" in report
    assert "distributions use 4 of 10 trades" in report


def test_strategy_name_cannot_break_markdown_structure():
    report = render_backtest_report(_run(strategy_name="x | y\n## injected <b>"))

    assert "\n## injected" not in report
    assert "| BTC trend" not in report
    assert "| Strategy name | x \\| y ## injected &lt;b&gt; |" in report


def test_resolve_report_language():
    assert resolve_report_language("zh_tw") == "zh-TW"
    assert resolve_report_language("zh") == "zh-CN"
    assert resolve_report_language("EN") == "en-US"
    assert resolve_report_language("ja-JP") is None
    assert resolve_report_language("") is None


# -- Agent Gateway ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    agent_auth._rate_state.clear()
    yield
    agent_auth._rate_state.clear()


def _authorize(monkeypatch, scopes: str = "R") -> None:
    token = {
        "id": 902,
        "user_id": TENANT_USER_ID,
        "name": "report-agent",
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


class _Repository:
    def __init__(self, rows):
        self.rows = rows
        self.get_calls: list[dict] = []

    def get_run(self, *, user_id, run_id):
        self.get_calls.append({"user_id": user_id, "run_id": run_id})
        for row in self.rows:
            if row["id"] == run_id and row["user_id"] == user_id:
                return copy.deepcopy(row)
        return None


@pytest.fixture
def repository(monkeypatch):
    repo = _Repository([_run(), _run(id=99, user_id=8, strategy_name="other tenant")])
    monkeypatch.setattr(backtest_runs, "get_backtest_run_repository", lambda: repo)
    monkeypatch.setattr(backtest_center, "get_strategy_backtest_repository", lambda: repo)
    return repo


def test_gateway_report_returns_shared_markdown(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs/41/report?language=zh-TW", headers=HEADERS)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data == {
        "runId": 41,
        "language": "zh-TW",
        "format": "markdown",
        "filename": "backtest-run-41.md",
        "markdown": render_backtest_report(_run(), language="zh-TW"),
    }
    assert repository.get_calls == [{"user_id": TENANT_USER_ID, "run_id": 41}]


def test_gateway_report_defaults_to_english(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs/41/report", headers=HEADERS)

    assert response.get_json()["data"]["language"] == "en-US"


def test_gateway_report_rejects_unsupported_language(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs/41/report?language=xx", headers=HEADERS)

    assert response.status_code == 400
    assert repository.get_calls == []


def test_gateway_report_other_tenant_is_not_found(client, monkeypatch, repository):
    _authorize(monkeypatch)

    response = client.get("/api/agent/v1/backtest/runs/99/report", headers=HEADERS)

    assert response.status_code == 404
    assert response.get_json()["data"] is None


def test_gateway_report_requires_read_scope(client, monkeypatch, repository):
    _authorize(monkeypatch, scopes="B")

    response = client.get("/api/agent/v1/backtest/runs/41/report", headers=HEADERS)

    assert response.status_code == 403
    assert repository.get_calls == []


# -- Backtest Center download ---------------------------------------------------


def _download(app, query: str, *, headers=None, user_id: int = TENANT_USER_ID):
    with app.test_request_context(f"/api/backtest/report?{query}", headers=headers or {}):
        g.user_id = user_id
        result = inspect.unwrap(backtest_center.download_strategy_backtest_report)()
    return result if isinstance(result, tuple) else (result, result.status_code)


def test_web_download_is_identical_to_gateway_markdown(app, repository):
    response, status = _download(app, "runId=41", headers={"X-App-Lang": "zh-TW"})

    assert status == 200
    assert response.mimetype == "text/markdown"
    assert response.headers["Content-Disposition"] == 'attachment; filename="backtest-run-41.md"'
    assert response.headers["Content-Language"] == "zh-TW"
    assert response.get_data(as_text=True) == render_backtest_report(_run(), language="zh-TW")
    assert repository.get_calls == [{"user_id": TENANT_USER_ID, "run_id": 41}]


def test_web_download_falls_back_to_english_for_other_languages(app, repository):
    response, status = _download(app, "runId=41", headers={"X-App-Lang": "ja-JP"})

    assert status == 200
    assert response.headers["Content-Language"] == "en-US"


def test_web_download_only_serves_own_runs(app, repository):
    response, status = _download(app, "runId=99")

    assert status == 404
    assert response.get_json()["msg"] == "strategyV2.runNotFound"


def test_web_download_requires_run_id(app, repository):
    response, status = _download(app, "runId=abc")

    assert status == 400
    assert repository.get_calls == []
