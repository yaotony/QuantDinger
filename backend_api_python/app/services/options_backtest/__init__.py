"""Offline TAIFEX option backtester (issue #67).

Standalone research tool over ``qd_tw_option_daily``: it is not wired into
Strategy API V2, ``submit_backtest``, paper or live trading, and option
contracts submitted there are still rejected with
``strategyV2.optionsBacktestUnsupported``. Results are research estimates, not
live-tradable performance. See ``docs/trading/TAIFEX_OPTIONS_BACKTEST.md``.
"""
from app.services.options_backtest.config import (
    BacktestConfig,
    CostConfig,
    LiquidityConfig,
    MarginConfig,
    config_from_mapping,
    txo_tick_size,
)
from app.services.options_backtest.engine import OptionsBacktester
from app.services.options_backtest.market import (
    MissingQuoteError,
    OptionMarket,
    OptionQuote,
    OptionsBacktestError,
    SettlementPriceMissing,
)
from app.services.options_backtest.report import format_summary
from app.services.options_backtest.strategy import parse_strategy


def run_backtest(market: OptionMarket, strategy_raw, start, end, *, cost_multiplier=None, sample: str = ""):
    """Parse a strategy mapping, run it on ``market`` and return the JSON-able result."""
    strategy = parse_strategy(strategy_raw)
    config = config_from_mapping(strategy.config or {}, cost_multiplier=cost_multiplier)
    result = OptionsBacktester(market, config).run(strategy, start, end)
    result["ledger"]["sample"] = sample
    return result


__all__ = [
    "BacktestConfig",
    "CostConfig",
    "LiquidityConfig",
    "MarginConfig",
    "MissingQuoteError",
    "OptionMarket",
    "OptionQuote",
    "OptionsBacktestError",
    "OptionsBacktester",
    "SettlementPriceMissing",
    "config_from_mapping",
    "format_summary",
    "parse_strategy",
    "run_backtest",
    "txo_tick_size",
]
