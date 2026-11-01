import pandas as pd
import pytest

from app.services.strategy_v2 import StrategyV2BacktestRunner
from app.services.strategy_v2.storage import compact_backtest_result
from app.services.strategy_v2.trade_diagnostics import (
    summarize_trade_diagnostics,
    trade_diagnostics,
)


def _frame(rows):
    index = pd.date_range("2026-01-01", periods=len(rows), freq="4h")
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close", "volume"])


LONG_WITH_STOP = """
def initialize(context):
    g.symbol = "Crypto:BTC/USDT"
    g.sent = False
    context.set_universe([g.symbol])
    context.subscribe(frequency="4h")

def handle_data(context, data):
    if not g.sent:
        order(g.symbol, 10, reason="entry", stop_loss_pct=0.02)
        g.sent = True
"""


SHORT_WITHOUT_STOP = """
def initialize(context):
    g.symbol = "Crypto:BTC/USDT@swap"
    g.step = 0
    context.set_universe([g.symbol])
    context.subscribe(frequency="4h")
    context.set_metadata(direction_mode="neutral")

def handle_data(context, data):
    if g.step == 0:
        order(g.symbol, -10, position_side="short", reason="short_entry")
        g.step = 1
    elif g.step == 1 and get_position(g.symbol, position_side="short").amount != 0:
        order(g.symbol, 10, position_side="short", reason="short_exit")
        g.step = 2
"""


def test_long_trade_reports_r_multiple_excursions_and_holding_time():
    frame = _frame([
        (100, 101, 99, 100, 1000),
        (100, 101, 99, 100, 1000),
        (100, 101, 97, 99, 1000),
        (99, 100, 98, 99, 1000),
    ])
    result = StrategyV2BacktestRunner(
        code=LONG_WITH_STOP,
        frames={"Crypto:BTC/USDT": frame},
        initial_capital=10_000,
        commission=0,
        slippage=0,
    ).run()

    trade = result["closedTrades"][0]
    assert trade["side"] == "long"
    assert trade["close_reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(98.0)
    assert trade["r_basis"] == "initial_stop_loss"
    assert trade["initial_stop_price"] == pytest.approx(98.0)
    assert trade["initial_risk"] == pytest.approx(20.0)
    assert trade["r_multiple"] == pytest.approx(-1.0)
    # Held through bar 1 (high 101, low 99) and stopped at 98 inside bar 2.
    assert trade["mae"] == pytest.approx(2.0)
    assert trade["mfe"] == pytest.approx(1.0)
    assert trade["mae_pct"] == pytest.approx(2.0)
    assert trade["mfe_pct"] == pytest.approx(1.0)
    assert trade["mae_r"] == pytest.approx(1.0)
    assert trade["mfe_r"] == pytest.approx(0.5)
    assert trade["holding_seconds"] == pytest.approx(4 * 3600)
    assert trade["holding_bars"] == 1
    assert trade["slippage_cost"] == pytest.approx(0.0)
    assert trade["profit_before_costs"] == pytest.approx(trade["profit"])

    assert result["averageR"] == pytest.approx(-1.0)
    assert result["rMultipleTradeCount"] == 1
    assert result["tradeDiagnosticsBasis"] == "all_closed_trades"
    assert result["expectancyAfterCosts"] == pytest.approx(-20.0)
    assert result["expectancyBeforeCosts"] == pytest.approx(-20.0)


def test_short_trade_without_stop_marks_r_not_applicable_and_splits_costs():
    frame = _frame([
        (100, 101, 99, 100, 100_000),
        (100, 103, 96, 99, 100_000),
        (95, 96, 94, 95, 100_000),
        (95, 96, 94, 95, 100_000),
    ])
    result = StrategyV2BacktestRunner(
        code=SHORT_WITHOUT_STOP,
        frames={"Crypto:BTC/USDT@swap": frame},
        initial_capital=10_000,
        commission=0.001,
        slippage=0.001,
    ).run()

    trade = result["closedTrades"][0]
    assert trade["side"] == "short"
    assert trade["entry_price"] == pytest.approx(99.9)
    assert trade["exit_price"] == pytest.approx(95.095)
    assert trade["r_basis"] == "not_applicable"
    assert trade["r_multiple"] is None
    assert trade["initial_risk"] is None
    assert trade["initial_stop_price"] is None
    assert trade["mae_r"] is None and trade["mfe_r"] is None
    # Adverse for a short is the bar-1 high; favorable reaches the exit fill.
    assert trade["mae"] == pytest.approx(103 - 99.9)
    assert trade["mfe"] == pytest.approx(99.9 - 95.095)
    assert trade["entry_slippage"] == pytest.approx(1.0)
    assert trade["exit_slippage"] == pytest.approx(0.95)
    assert trade["slippage_cost"] == pytest.approx(1.95)
    assert trade["trading_cost"] == pytest.approx(trade["commission"] + 1.95)
    assert trade["profit_before_costs"] == pytest.approx(50.0)
    assert trade["holding_seconds"] == pytest.approx(4 * 3600)

    assert result["averageR"] is None
    assert result["rMultipleTradeCount"] == 0
    assert result["totalSlippageCost"] == pytest.approx(1.95)
    assert result["expectancyBeforeCosts"] == pytest.approx(50.0)
    assert result["expectancyAfterCosts"] == pytest.approx(trade["profit"])
    assert result["costToGrossProfitPct"] == pytest.approx(trade["trading_cost"] / 50.0 * 100)
    assert result["costToGrossLossPct"] is None


def test_short_trade_with_stop_measures_risk_above_entry():
    diagnostics = trade_diagnostics(
        side="short",
        entry_price=200.0,
        quantity=2.0,
        highest_price=206.0,
        lowest_price=190.0,
        stop_loss_pct=0.05,
        profit=15.0,
        commission=1.0,
        entry_slippage=0.5,
        exit_slippage=0.5,
        entry_time="2026-01-01T00:00:00Z",
        exit_time="2026-01-02T00:00:00Z",
        holding_bars=6,
    )
    assert diagnostics["initial_stop_price"] == pytest.approx(210.0)
    assert diagnostics["initial_risk"] == pytest.approx(20.0)
    assert diagnostics["r_multiple"] == pytest.approx(0.75)
    assert diagnostics["mae"] == pytest.approx(6.0)
    assert diagnostics["mfe"] == pytest.approx(10.0)
    assert diagnostics["mae_r"] == pytest.approx(0.6)
    assert diagnostics["mfe_r"] == pytest.approx(1.0)
    assert diagnostics["holding_seconds"] == pytest.approx(86400)
    assert diagnostics["trading_cost"] == pytest.approx(2.0)
    assert diagnostics["profit_before_costs"] == pytest.approx(17.0)


def test_summary_reports_cost_ratios_against_pre_cost_gross_profit_and_loss():
    trades = [
        {"profit": 90.0, "commission": 6.0, "slippage_cost": 4.0,
         "profit_before_costs": 100.0, "r_multiple": 2.0, "holding_seconds": 100.0},
        {"profit": -60.0, "commission": 6.0, "slippage_cost": 4.0,
         "profit_before_costs": -50.0, "r_multiple": -1.0, "holding_seconds": 300.0},
        {"profit": 10.0, "commission": 0.0, "slippage_cost": 0.0,
         "profit_before_costs": 10.0, "r_multiple": None, "holding_seconds": 200.0},
    ]
    summary = summarize_trade_diagnostics(trades)
    assert summary["averageR"] == pytest.approx(0.5)
    assert summary["rMultipleTradeCount"] == 2
    assert summary["totalTradingCost"] == pytest.approx(20.0)
    assert summary["grossProfitBeforeCosts"] == pytest.approx(110.0)
    assert summary["grossLossBeforeCosts"] == pytest.approx(50.0)
    assert summary["costToGrossProfitPct"] == pytest.approx(20.0 / 110.0 * 100)
    assert summary["costToGrossLossPct"] == pytest.approx(40.0)
    assert summary["expectancyBeforeCosts"] == pytest.approx(60.0 / 3)
    assert summary["expectancyAfterCosts"] == pytest.approx(40.0 / 3)
    assert summary["averageHoldingSeconds"] == pytest.approx(200.0)


def test_summary_survives_closed_trade_thinning():
    trades = [
        {
            "profit": float(index % 7) - 3.0,
            "commission": 0.1,
            "slippage_cost": 0.05,
            "profit_before_costs": float(index % 7) - 2.85,
            "r_multiple": (float(index % 7) - 3.0) / 2.0 if index % 3 else None,
            "holding_seconds": float(index),
        }
        for index in range(2500)
    ]
    full_summary = summarize_trade_diagnostics(trades)
    compact = compact_backtest_result({"closedTrades": trades, **full_summary})

    assert len(compact["closedTrades"]) < len(trades)
    for key, value in full_summary.items():
        assert compact[key] == value
    assert compact["tradeDiagnosticsTradeCount"] == 2500
    thinned_summary = summarize_trade_diagnostics(compact["closedTrades"])
    assert thinned_summary["tradeDiagnosticsTradeCount"] != compact["tradeDiagnosticsTradeCount"]
