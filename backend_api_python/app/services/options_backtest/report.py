"""Metrics and the research-ledger record of an offline option backtest.

``ledger`` is a flat object of scalars so a research ledger tool can store it as
one row. Every result carries ``not_live_performance: true``: daily data has no
bid/ask and margins are approximated, so the numbers are research estimates.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from app.services.options_backtest.engine import OptionsBacktester
    from app.services.options_backtest.strategy import StrategySpec

ENGINE = "taifex-options-offline-v1"
DISCLAIMER = (
    "Offline research estimate from TAIFEX daily data (no bid/ask, approximated margin). "
    "Not live-tradable performance."
)


def max_drawdown(initial: float, equities: List[float]) -> float:
    """Largest peak-to-trough fall as a fraction of the peak; the peak starts at ``initial``."""
    peak, worst = float(initial), 0.0
    for value in equities:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _round(value: Optional[float], digits: int = 10) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def summary_metrics(bt: "OptionsBacktester") -> Dict[str, Any]:
    initial = float(bt.config.initial_capital)
    equities = [point["equity"] for point in bt.curve]
    final_equity = equities[-1] if equities else initial
    pnls = [trade["pnl"] for trade in bt.closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    total_cost = sum(bt.cost_totals.values())
    return {
        "initial_capital": initial,
        "final_equity": _round(final_equity, 4),
        "total_return": _round(final_equity / initial - 1.0),
        "max_drawdown": _round(max_drawdown(initial, equities)),
        "trades": len(pnls),
        "win_rate": _round(len(wins) / len(pnls)) if pnls else None,
        "total_cost": _round(total_cost, 4),
        "cost_breakdown": {key: _round(value, 4) for key, value in bt.cost_totals.items()},
        "max_single_loss": _round(min(pnls), 4) if pnls and min(pnls) < 0 else 0.0,
        "peak_margin": _round(max((point["margin"] for point in bt.curve), default=0.0), 4),
        "open_positions_at_end": len(bt.open),
        "rejected_entries": sum(1 for event in bt.events if event["type"] == "entry_rejected"),
        "blocked_exits": sum(1 for event in bt.events if event["type"] == "exit_blocked"),
        "margin_shortfall_days": sum(1 for event in bt.events if event["type"] == "margin_shortfall"),
    }


def ledger_record(strategy_name: str, start: date, end: date, cost_multiplier: float,
                  metrics: Dict[str, Any], sample: str = "") -> Dict[str, Any]:
    return {
        "engine": ENGINE,
        "strategy": strategy_name,
        "market": "TWFutures",
        "product": "TXO",
        "sample": sample,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cost_multiplier": cost_multiplier,
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "trades": metrics["trades"],
        "win_rate": metrics["win_rate"],
        "total_cost": metrics["total_cost"],
        "max_single_loss": metrics["max_single_loss"],
        "peak_margin": metrics["peak_margin"],
        "final_equity": metrics["final_equity"],
        "open_positions_at_end": metrics["open_positions_at_end"],
        "not_live_performance": True,
    }


def build_result(bt: "OptionsBacktester", strategy: "StrategySpec", start: date, end: date) -> Dict[str, Any]:
    from app.services.options_backtest.engine import trade_record

    metrics = summary_metrics(bt)
    last_mark = bt.curve[-1]["date"] if bt.curve else None
    return {
        "engine": ENGINE,
        "strategy": strategy.name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "not_live_performance": True,
        "disclaimer": DISCLAIMER,
        "config": bt.config.as_dict(),
        "metrics": metrics,
        "ledger": ledger_record(strategy.name, start, end, bt.config.costs.cost_multiplier, metrics),
        "trades": list(bt.closed),
        "open_positions": [dict(trade_record(p, bt.multiplier), marked_on=last_mark) for p in bt.open],
        "equity_curve": list(bt.curve),
        "events": list(bt.events),
    }


def format_summary(result: Dict[str, Any]) -> str:
    """Short human-readable summary for the CLI."""
    m = result["metrics"]

    def pct(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value * 100:.2f}%"

    lines = [
        f"Strategy {result['strategy']} {result['start']}..{result['end']} (engine {result['engine']})",
        f"  total return {pct(m['total_return'])}, max drawdown {pct(m['max_drawdown'])}",
        f"  trades {m['trades']}, win rate {pct(m['win_rate'])}, max single loss {m['max_single_loss']:,.2f}",
        f"  total cost {m['total_cost']:,.2f} (x{result['config']['costs']['cost_multiplier']:g}), "
        f"peak margin {m['peak_margin']:,.2f}",
        f"  open at end {m['open_positions_at_end']}, rejected entries {m['rejected_entries']}, "
        f"blocked exits {m['blocked_exits']}, margin shortfall days {m['margin_shortfall_days']}",
        "  " + DISCLAIMER,
    ]
    return "\n".join(lines)
