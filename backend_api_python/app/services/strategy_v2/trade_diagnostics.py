"""Per-trade diagnostics and their run-level summary for Strategy API V2 backtests.

Definitions (documented in docs/trading/STRATEGY_DEV_GUIDE.md):

* 1R is the initial stop distance: ``entry_price * stop_loss_pct`` per unit,
  taken from the first protection that carried a stop loss while the entry
  was open. Trades without a stop loss report ``r_multiple = None`` and
  ``r_basis = "not_applicable"``.
* ``r_multiple`` is the net profit (after commission and slippage) divided by
  the initial risk ``1R * quantity``.
* MAE/MFE are the largest adverse/favorable per-unit price moves from the
  entry price, observed on completed bars while the position was held plus
  the entry and exit fill prices. Granularity is one driving bar.
* Slippage cost is ``|fill_price - reference_price| * quantity`` for fills
  priced from a bar open (limit and gap-open fills carry no modeled slippage).
* ``profit_before_costs`` adds commission and slippage back to the net profit.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import pandas as pd


def slippage_per_unit(execution: Mapping[str, Any]) -> float:
    """Return the modeled slippage paid per unit by one execution."""
    if str(execution.get("fill_reference") or "bar_open") != "bar_open":
        return 0.0
    reference = float(execution.get("reference_price") or 0.0)
    price = float(execution.get("price") or 0.0)
    if reference <= 0 or price <= 0:
        return 0.0
    return abs(price - reference)


def holding_seconds(entry_time: Any, exit_time: Any) -> float | None:
    try:
        seconds = (pd.Timestamp(exit_time) - pd.Timestamp(entry_time)).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(0.0, float(seconds))


def trade_diagnostics(
    *,
    side: str,
    entry_price: float,
    quantity: float,
    highest_price: float,
    lowest_price: float,
    stop_loss_pct: float,
    profit: float,
    commission: float,
    entry_slippage: float,
    exit_slippage: float,
    entry_time: Any,
    exit_time: Any,
    holding_bars: int,
) -> dict[str, Any]:
    """Build the diagnostic fields appended to one closed trade."""
    is_long = str(side).lower() != "short"
    entry = float(entry_price or 0.0)
    high = max(float(highest_price or entry), entry)
    low = min(float(lowest_price or entry), entry) if entry > 0 else float(lowest_price or 0.0)
    favorable = max(0.0, (high - entry) if is_long else (entry - low))
    adverse = max(0.0, (entry - low) if is_long else (high - entry))
    slippage_cost = max(0.0, float(entry_slippage or 0.0)) + max(0.0, float(exit_slippage or 0.0))
    fees = max(0.0, float(commission or 0.0))
    net_profit = float(profit or 0.0)

    pct = max(0.0, float(stop_loss_pct or 0.0))
    risk_per_unit = entry * pct if entry > 0 and pct > 0 else 0.0
    initial_risk = risk_per_unit * float(quantity or 0.0)
    has_r = risk_per_unit > 0 and initial_risk > 0

    return {
        "r_basis": "initial_stop_loss" if has_r else "not_applicable",
        "initial_stop_price": (
            entry * (1.0 - pct if is_long else 1.0 + pct) if has_r else None
        ),
        "initial_risk": initial_risk if has_r else None,
        "r_multiple": net_profit / initial_risk if has_r else None,
        "mae": adverse,
        "mfe": favorable,
        "mae_pct": adverse / entry * 100.0 if entry > 0 else 0.0,
        "mfe_pct": favorable / entry * 100.0 if entry > 0 else 0.0,
        "mae_r": adverse / risk_per_unit if has_r else None,
        "mfe_r": favorable / risk_per_unit if has_r else None,
        "holding_seconds": holding_seconds(entry_time, exit_time),
        "holding_bars": max(0, int(holding_bars or 0)),
        "entry_slippage": max(0.0, float(entry_slippage or 0.0)),
        "exit_slippage": max(0.0, float(exit_slippage or 0.0)),
        "slippage_cost": slippage_cost,
        "trading_cost": fees + slippage_cost,
        "profit_before_costs": net_profit + fees + slippage_cost,
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_trade_diagnostics(trades: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize diagnostics over every closed trade.

    Must be called on the complete trade list, before any history thinning,
    so stored and delivered summaries do not depend on sampled rows.
    """
    rows = [item for item in trades if isinstance(item, Mapping)]
    net = [float(item.get("profit") or 0.0) for item in rows]
    before = [
        float(
            item.get("profit_before_costs")
            if item.get("profit_before_costs") is not None
            else float(item.get("profit") or 0.0) + float(item.get("commission") or 0.0)
        )
        for item in rows
    ]
    commission = sum(float(item.get("commission") or 0.0) for item in rows)
    slippage = sum(float(item.get("slippage_cost") or 0.0) for item in rows)
    trading_cost = commission + slippage
    r_values = [float(item["r_multiple"]) for item in rows if item.get("r_multiple") is not None]
    holding = [float(item["holding_seconds"]) for item in rows if item.get("holding_seconds") is not None]
    mae_pct = [float(item["mae_pct"]) for item in rows if item.get("mae_pct") is not None]
    mfe_pct = [float(item["mfe_pct"]) for item in rows if item.get("mfe_pct") is not None]
    gross_profit_before = sum(value for value in before if value > 0)
    gross_loss_before = abs(sum(value for value in before if value < 0))
    return {
        "tradeDiagnosticsBasis": "all_closed_trades",
        "tradeDiagnosticsTradeCount": len(rows),
        "averageR": _mean(r_values),
        "rMultipleTradeCount": len(r_values),
        "totalTradeCommission": commission,
        "totalSlippageCost": slippage,
        "totalTradingCost": trading_cost,
        "grossProfitBeforeCosts": gross_profit_before,
        "grossLossBeforeCosts": gross_loss_before,
        "costToGrossProfitPct": (
            trading_cost / gross_profit_before * 100.0 if gross_profit_before > 0 else None
        ),
        "costToGrossLossPct": (
            trading_cost / gross_loss_before * 100.0 if gross_loss_before > 0 else None
        ),
        "expectancyBeforeCosts": _mean(before),
        "expectancyAfterCosts": _mean(net),
        "averageHoldingSeconds": _mean(holding),
        "averageMaePct": _mean(mae_pct),
        "averageMfePct": _mean(mfe_pct),
    }
