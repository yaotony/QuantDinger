"""Offline TXO option backtester (issue #67): expiry, costs, margin, liquidity, hand-checked example.

Every test builds an in-memory chain; nothing touches PostgreSQL or TAIFEX.
Expiry dates are injected (the August 2026 TXO expires on Wednesday 2026-08-19).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from app.services.options_backtest import (
    BacktestConfig,
    CostConfig,
    LiquidityConfig,
    MarginConfig,
    MissingQuoteError,
    OptionMarket,
    OptionQuote,
    OptionsBacktester,
    SettlementPriceMissing,
    config_from_mapping,
    parse_strategy,
    run_backtest,
    txo_tick_size,
)
from app.services.options_backtest.strategy import Leg, max_loss, structure_margin

EXPIRIES = {"202608": date(2026, 8, 19), "202609": date(2026, 9, 16)}
ZERO_COSTS = dict(fee_per_contract=0, settlement_fee_per_contract=0, tax_rate=0, settlement_tax_rate=0,
                  slippage_ticks=0)


def q(day, strike, right, close, settlement=None, *, volume=1000, oi=5000, expiry="202608"):
    return OptionQuote(date.fromisoformat(day), expiry, float(strike), right, close,
                       close if settlement is None else settlement, volume, oi)


def market(quotes, settlements=None):
    return OptionMarket(quotes, settlements if settlements is not None else {"202608": 23150.0},
                        expiry_dates=EXPIRIES)


def config(capital=100_000.0, *, margin=None, liquidity=None, **costs):
    return BacktestConfig(
        initial_capital=capital,
        costs=CostConfig(**costs),
        liquidity=liquidity or LiquidityConfig(min_volume=20, min_open_interest=100),
        margin=margin or MarginConfig(margin_a=85_000, margin_b=43_000),
    )


def trade(structure, legs, entry="2026-08-03", expiry="202608", quantity=1, exit_date=None):
    raw = {"entry_date": entry, "expiry": expiry, "structure": structure, "legs": legs, "quantity": quantity}
    if exit_date:
        raw["exit_date"] = exit_date
    return parse_strategy({"name": "test", "trades": [raw]})


def leg(side, right, strike):
    return {"side": side, "right": right, "strike": strike}


def run(quotes, strategy, cfg, settlements=None, end="2026-08-31"):
    return OptionsBacktester(market(quotes, settlements), cfg).run(
        strategy, date(2026, 8, 1), date.fromisoformat(end))


LONG_CALL_QUOTES = [
    q("2026-08-03", 23000, "call", 100),
    q("2026-08-10", 23000, "call", 80),
    q("2026-08-19", 23000, "call", 150),
]


# --------------------------------------------------------------------------- hand-calculated example
def test_single_long_call_from_entry_to_expiry_matches_hand_calculation():
    """One TXO 202608 23000 call, bought 2026-08-03, held to cash settlement on 2026-08-19.

    Defaults: NT$50 per point, tax 0.1% of premium, fee NT$25 per contract, 1 tick slippage.
    Entry  (close 100, tick 1): fill 100 + 1 = 101 points
           premium 101 x 50 = 5,050; fee 25; tax 5,050 x 0.001 = 5.05
           cash 100,000 - 5,050 - 25 - 5.05 = 94,919.95
           equity 08-03 = 94,919.95 + 100 (settlement) x 50 = 99,919.95
    08-10  settlement 80: equity = 94,919.95 + 80 x 50 = 98,919.95
           drawdown from the 100,000 start = 1,080.05 / 100,000 = 1.080050%
    Expiry final settlement 23,150: intrinsic 150 points -> 150 x 50 = 7,500
           settlement tax 7,500 x 0.001 = 7.5; settlement fee 25
           cash 94,919.95 + 7,500 - 7.5 - 25 = 102,387.45
    Trade pnl 102,387.45 - 100,000 = 2,387.45; total return 2.38745%
    Costs: fees 25 + 25 = 50; taxes 5.05 + 7.5 = 12.55; slippage 1 x 50 = 50 -> 112.55
    Max loss of the structure = premium paid = 5,050.
    """
    strategy = trade("long_call", [leg("buy", "call", 23000)])
    result = run(LONG_CALL_QUOTES, strategy, config())

    assert [p["equity"] for p in result["equity_curve"]] == [99_919.95, 98_919.95, 102_387.45]
    (t,) = result["trades"]
    assert t["entry_prices"] == [101.0]
    assert t["exit_reason"] == "expiry" and t["exit_prices"] == [150.0]
    assert t["final_settlement_price"] == 23150.0
    assert t["pnl"] == pytest.approx(2_387.45)
    assert t["max_loss"] == pytest.approx(5_050.0) and t["max_loss_unlimited"] is False

    m = result["metrics"]
    assert m["final_equity"] == pytest.approx(102_387.45)
    assert m["total_return"] == pytest.approx(0.0238745)
    assert m["max_drawdown"] == pytest.approx(0.0108005)
    assert m["trades"] == 1 and m["win_rate"] == 1.0
    assert m["total_cost"] == pytest.approx(112.55)
    assert m["cost_breakdown"] == pytest.approx({"fee": 50.0, "tax": 12.55, "slippage": 50.0})
    assert m["max_single_loss"] == 0.0
    assert m["peak_margin"] == 0.0
    assert result["open_positions"] == []
    assert result["not_live_performance"] is True


def test_double_cost_multiplier_scales_fees_taxes_and_slippage():
    """cost_multiplier 2: slippage 2 ticks -> fill 102; premium 5,100; fee 50; tax 5,100 x 0.001 x 2 = 10.2
    cash 100,000 - 5,100 - 50 - 10.2 = 94,839.8; expiry +7,500 - tax 15 - fee 50 = 102,274.8
    costs: fees 100, taxes 25.2, slippage 2 x 50 = 100 -> 225.2
    """
    strategy = trade("long_call", [leg("buy", "call", 23000)])
    result = run(LONG_CALL_QUOTES, strategy, config(cost_multiplier=2.0))
    assert result["trades"][0]["entry_prices"] == [102.0]
    assert result["metrics"]["final_equity"] == pytest.approx(102_274.8)
    assert result["metrics"]["cost_breakdown"] == pytest.approx({"fee": 100.0, "tax": 25.2, "slippage": 100.0})
    assert result["ledger"]["cost_multiplier"] == 2.0


def test_cost_parameters_multiplier_tax_fee_and_slippage():
    # Two contracts sold at close 60 (tick 1) with 2 ticks slippage: fill 58.
    strategy = trade("short_put", [leg("sell", "put", 22000)], quantity=2)
    quotes = [q("2026-08-03", 22000, "put", 60), q("2026-08-03", 23000, "call", 100),
              q("2026-08-03", 23000, "put", 100)]
    cfg = config(1_000_000, multiplier=50, tax_rate=0.001, fee_per_contract=30, slippage_ticks=2)
    result = run(quotes, strategy, cfg, end="2026-08-03")
    # premium 58 x 50 x 2 = 5,800; fee 30 x 2 = 60; tax 5.8; slippage 2 x 50 x 2 = 200
    assert result["open_positions"][0]["entry_prices"] == [58.0]
    assert result["open_positions"][0]["costs"] == pytest.approx({"fee": 60.0, "tax": 5.8, "slippage": 200.0})
    assert result["equity_curve"][0]["cash"] == pytest.approx(1_000_000 + 5_800 - 60 - 5.8)
    # Marked at settlement 60: equity = cash - 60 x 50 x 2
    assert result["equity_curve"][0]["equity"] == pytest.approx(1_000_000 + 5_800 - 65.8 - 6_000)


def test_tick_sizes_and_sell_fill_never_goes_negative():
    assert [txo_tick_size(p) for p in (0.5, 9.9, 10, 49.5, 50, 499, 500, 999, 1000)] == [
        0.1, 0.1, 0.5, 0.5, 1, 1, 5, 5, 10]
    bt = OptionsBacktester(market([]), config(slippage_ticks=5))
    assert bt._fill(0.2, "sell", 1).price == 0.0


# --------------------------------------------------------------------------- expiry
def test_in_the_money_put_settles_at_intrinsic_value():
    strategy = trade("long_put", [leg("buy", "put", 23000)])
    quotes = [q("2026-08-03", 23000, "put", 50), q("2026-08-19", 23000, "put", 200)]
    result = run(quotes, strategy, config(**ZERO_COSTS), settlements={"202608": 22800.0})
    (t,) = result["trades"]
    assert t["exit_prices"] == [200.0]
    assert t["pnl"] == pytest.approx((200 - 50) * 50)


def test_out_of_the_money_option_expires_worthless_without_settlement_costs():
    strategy = trade("long_call", [leg("buy", "call", 23500)])
    quotes = [q("2026-08-03", 23500, "call", 20), q("2026-08-19", 23500, "call", 0.1)]
    result = run(quotes, strategy, config())
    (t,) = result["trades"]
    assert t["exit_prices"] == [0.0]
    # Entry: fill 20.5 (tick 0.5) -> 1,025 + fee 25 + tax 1.025; nothing is charged at a worthless expiry.
    assert t["pnl"] == pytest.approx(-(1_025 + 25 + 1.025))
    assert t["costs"]["fee"] == 25.0
    assert result["metrics"]["max_single_loss"] == pytest.approx(-1_051.025)
    assert result["metrics"]["win_rate"] == 0.0


def test_both_spread_legs_settle_on_the_same_expiry():
    strategy = trade("bull_call_spread", [leg("buy", "call", 23000), leg("sell", "call", 23200)])
    quotes = [q("2026-08-03", 23000, "call", 100), q("2026-08-03", 23200, "call", 40),
              q("2026-08-19", 23000, "call", 300), q("2026-08-19", 23200, "call", 100)]
    result = run(quotes, strategy, config(**ZERO_COSTS), settlements={"202608": 23300.0})
    (t,) = result["trades"]
    assert t["exit_date"] == "2026-08-19" and t["exit_prices"] == [300.0, 100.0]
    assert t["pnl"] == pytest.approx((300 - 100 - (100 - 40)) * 50)
    assert t["max_loss"] == pytest.approx(60 * 50)
    assert result["open_positions"] == [] and result["equity_curve"][-1]["open_positions"] == 0


def test_missing_final_settlement_price_is_an_error_not_zero():
    strategy = trade("long_call", [leg("buy", "call", 23000)])
    with pytest.raises(SettlementPriceMissing, match="202608"):
        run(LONG_CALL_QUOTES, strategy, config(), settlements={})


def test_a_position_without_a_daily_settlement_price_stops_the_run():
    strategy = trade("long_call", [leg("buy", "call", 23000)])
    quotes = [q("2026-08-03", 23000, "call", 100), q("2026-08-04", 23200, "call", 50)]
    with pytest.raises(MissingQuoteError, match="2026-08-04"):
        run(quotes, strategy, config())


def test_positions_open_at_the_end_are_reported_not_dropped():
    strategy = trade("long_call", [leg("buy", "call", 23000)])
    result = run(LONG_CALL_QUOTES[:2], strategy, config(), end="2026-08-10")
    assert result["metrics"]["trades"] == 0 and result["metrics"]["open_positions_at_end"] == 1
    assert result["open_positions"][0]["marked_on"] == "2026-08-10"
    assert result["ledger"]["open_positions_at_end"] == 1


# --------------------------------------------------------------------------- margin
PARITY = [q("2026-08-03", 23000, "call", 100), q("2026-08-03", 23000, "put", 110)]  # S = 23000 + 100 - 110


def test_short_put_uses_margin_and_tracks_the_peak():
    strategy = trade("short_put", [leg("sell", "put", 22500)])
    quotes = PARITY + [q("2026-08-03", 22500, "put", 60)]
    result = run(quotes, strategy, config(1_000_000, **ZERO_COSTS), end="2026-08-03")
    # Underlying 22,990; out-of-money 490 pts = 24,500; max(85,000 - 24,500, 43,000) + 60 x 50 = 63,500
    assert result["equity_curve"][0]["margin"] == pytest.approx(63_500)
    assert result["metrics"]["peak_margin"] == pytest.approx(63_500)
    assert result["open_positions"][0]["max_loss"] == pytest.approx((22500 - 60) * 50)


def test_insufficient_margin_rejects_the_entry():
    strategy = trade("short_put", [leg("sell", "put", 22500)])
    quotes = PARITY + [q("2026-08-03", 22500, "put", 60)]
    result = run(quotes, strategy, config(50_000, **ZERO_COSTS), end="2026-08-03")
    assert result["open_positions"] == [] and result["metrics"]["rejected_entries"] == 1
    (event,) = [e for e in result["events"] if e["type"] == "entry_rejected"]
    assert "insufficient margin" in event["reason"]
    assert result["equity_curve"][0]["cash"] == 50_000 and result["equity_curve"][0]["margin"] == 0


def test_margin_of_spreads_and_condor_and_unlimited_short_call():
    m = MarginConfig(margin_a=85_000, margin_b=43_000)
    bull_put = [Leg("buy", "put", 22400), Leg("sell", "put", 22600)]
    assert structure_margin("bull_put_spread", bull_put, [20, 40], 23000, 50, 2, m) == 200 * 50 * 2
    condor = [Leg("buy", "put", 22400), Leg("sell", "put", 22600), Leg("sell", "call", 23400),
              Leg("buy", "call", 23700)]
    assert structure_margin("iron_condor", condor, [10, 20, 20, 10], 23000, 50, 1, m) == 300 * 50
    assert structure_margin("bull_call_spread", [Leg("buy", "call", 1), Leg("sell", "call", 2)],
                            [1, 1], None, 50, 1, m) == 0
    # Deep out of the money: B is the floor.
    assert structure_margin("short_call", [Leg("sell", "call", 25000)], [1], 23000, 50, 1, m) == 50 + 43_000
    assert max_loss([Leg("sell", "call", 25000)], [10], 50, 1) is None
    assert max_loss(condor, [10, 20, 20, 10], 50, 1) == pytest.approx((300 - 20) * 50)


# --------------------------------------------------------------------------- liquidity
@pytest.mark.parametrize("volume,oi", [(19, 5000), (1000, 99)])
def test_illiquid_strikes_cannot_be_traded(volume, oi):
    strategy = trade("long_call", [leg("buy", "call", 23000)])
    quotes = [q("2026-08-03", 23000, "call", 100, volume=volume, oi=oi)]
    result = run(quotes, strategy, config(), end="2026-08-03")
    assert result["open_positions"] == []
    (event,) = result["events"]
    assert event["type"] == "entry_rejected" and event["reason"].startswith("illiquid 202608 call 23000")


def test_illiquid_exit_keeps_the_position_until_expiry():
    # The exit is retried every day up to the expiry day; while illiquid the position stays open.
    strategy = trade("long_call", [leg("buy", "call", 23000)], exit_date="2026-08-10")
    quotes = [q("2026-08-03", 23000, "call", 100), q("2026-08-10", 23000, "call", 80, volume=0),
              q("2026-08-19", 23000, "call", 150, oi=10)]
    result = run(quotes, strategy, config())
    assert [e["type"] for e in result["events"]] == ["entry", "exit_blocked", "exit_blocked", "expiry"]
    assert result["trades"][0]["exit_reason"] == "expiry"
    assert result["metrics"]["blocked_exits"] == 2


def test_blocked_exit_is_retried_on_the_next_liquid_day():
    strategy = trade("long_call", [leg("buy", "call", 23000)], exit_date="2026-08-10")
    quotes = [q("2026-08-03", 23000, "call", 100), q("2026-08-10", 23000, "call", 80, volume=0),
              q("2026-08-11", 23000, "call", 90), q("2026-08-19", 23000, "call", 150)]
    result = run(quotes, strategy, config(**ZERO_COSTS))
    (t,) = result["trades"]
    assert (t["exit_reason"], t["exit_date"], t["exit_prices"]) == ("exit", "2026-08-11", [90.0])


def test_liquid_exit_closes_before_expiry():
    strategy = trade("long_call", [leg("buy", "call", 23000)], exit_date="2026-08-10")
    result = run(LONG_CALL_QUOTES, strategy, config(**ZERO_COSTS))
    (t,) = result["trades"]
    assert (t["exit_reason"], t["exit_date"], t["exit_prices"]) == ("exit", "2026-08-10", [80.0])
    assert t["pnl"] == pytest.approx((80 - 100) * 50)


# --------------------------------------------------------------------------- rule strategies
def _rule_quotes():
    out = []
    for day in ("2026-07-20", "2026-07-21"):
        out += [q(day, 23000, "call", 150), q(day, 23000, "put", 150)]
        out += [q(day, k, "put", p) for k, p in ((22600, 30), (22800, 60))]
    out += [q("2026-08-19", k, r, 0.1) for k, r in ((23000, "call"), (23000, "put"), (22600, "put"),
                                                  (22800, "put"))]
    return out


def test_rule_opens_one_bull_put_spread_per_expiry_from_strike_offsets():
    raw = {
        "name": "bull-put",
        "config": {"initial_capital": 200_000, "costs": ZERO_COSTS},
        "rule": {"structure": "bull_put_spread", "quantity": 1,
                 "legs": [{"side": "buy", "right": "put", "strike_offset": -400},
                          {"side": "sell", "right": "put", "strike_offset": -200}],
                 "entry_days_before_expiry": 30, "min_days_to_expiry": 7},
    }
    result = run_backtest(market(_rule_quotes()), raw, date(2026, 7, 1), date(2026, 8, 31), sample="in")
    (t,) = result["trades"]
    assert t["entry_date"] == "2026-07-20"
    assert [leg_["strike"] for leg_ in t["legs"]] == [22600.0, 22800.0]
    assert t["pnl"] == pytest.approx((60 - 30) * 50)  # both legs expire worthless at 23,150
    assert result["equity_curve"][0]["margin"] == pytest.approx(200 * 50)
    assert result["ledger"]["sample"] == "in" and result["ledger"]["strategy"] == "bull-put"


def test_strategy_validation():
    with pytest.raises(ValueError, match="exactly one"):
        parse_strategy({"name": "x"})
    with pytest.raises(ValueError, match="legs must be"):
        trade("bull_call_spread", [leg("sell", "call", 23000), leg("buy", "call", 23200)])
    with pytest.raises(ValueError, match="unsupported structure"):
        trade("straddle", [leg("buy", "call", 23000)])
    with pytest.raises(ValueError, match="monthly contract"):
        trade("long_call", [leg("buy", "call", 23000)], expiry="202608W1")
    with pytest.raises(ValueError, match="only TXO"):
        config_from_mapping({"product": "TEO"})
    with pytest.raises(ValueError, match="unknown CostConfig"):
        config_from_mapping({"costs": {"commission": 1}})
    assert config_from_mapping({}, cost_multiplier=2).costs.cost_multiplier == 2.0


def test_wrong_strike_order_is_rejected_at_entry():
    strategy = trade("bull_call_spread", [leg("buy", "call", 23200), leg("sell", "call", 23000)])
    quotes = [q("2026-08-03", 23000, "call", 100), q("2026-08-03", 23200, "call", 40)]
    result = run(quotes, strategy, config(), end="2026-08-03")
    assert result["events"][0]["type"] == "entry_rejected" and "do not form" in result["events"][0]["reason"]


# --------------------------------------------------------------------------- isolation and CLI
def test_offline_backtester_is_not_wired_into_strategy_v2():
    """#59's rejection of option contracts in Strategy API V2 backtests is unchanged."""
    from app.data_sources.tw_options import OPTIONS_UNSUPPORTED_ERROR
    from app.services.strategy_v2 import service as strategy_service
    from app.services.strategy_v2.contract import StrategyV2ContractError

    assert OPTIONS_UNSUPPORTED_ERROR == "strategyV2.optionsBacktestUnsupported"
    with pytest.raises(StrategyV2ContractError, match="strategyV2.optionsBacktestUnsupported"):
        strategy_service.reject_tw_options([{"market": "TWFutures", "symbol": "TXO23000H6"}])
    package = Path(__file__).resolve().parents[1] / "app" / "services" / "options_backtest"
    for source in package.glob("*.py"):
        assert "strategy_v2" not in source.read_text(encoding="utf-8"), source.name


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_options_backtest.py"
    spec = importlib.util.spec_from_file_location("run_options_backtest", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_options_backtest", module)
    spec.loader.exec_module(module)
    return module


def test_cli_writes_json_with_a_ledger_record(tmp_path, capsys):
    cli = _load_cli()
    strategy_file = tmp_path / "strategy.json"
    strategy_file.write_text(json.dumps({
        "name": "one-call",
        "config": {"initial_capital": 100_000},
        "trades": [{"entry_date": "2026-08-03", "expiry": "202608", "structure": "long_call",
                    "legs": [{"side": "buy", "right": "call", "strike": 23000}]}],
    }), encoding="utf-8")
    output = tmp_path / "result.json"
    loader_calls = []

    def loader(product, start, end):
        loader_calls.append((product, start, end))
        return market(LONG_CALL_QUOTES)

    code = cli.run([str(strategy_file), "--from", "2026-08-01", "--to", "2026-08-31", "--cost-multiplier", "2",
                    "--sample", "out", "--output", str(output)], market_loader=loader)
    assert code == 0
    assert loader_calls == [("TXO", date(2026, 8, 1), date(2026, 8, 31))]
    ledger = json.loads(output.read_text(encoding="utf-8"))["ledger"]
    for key in ("total_return", "max_drawdown", "trades", "win_rate", "total_cost", "max_single_loss",
                "peak_margin"):
        assert key in ledger
    assert ledger["cost_multiplier"] == 2.0 and ledger["sample"] == "out" and ledger["not_live_performance"]
    assert "Not live-tradable performance" in capsys.readouterr().err

    # A missing final settlement is reported as a failure, not a zero settlement.
    code = cli.run([str(strategy_file), "--from", "2026-08-01", "--to", "2026-08-31"],
                   market_loader=lambda *a: market(LONG_CALL_QUOTES, settlements={}))
    assert code == 1
    assert "final settlement price missing" in capsys.readouterr().err
