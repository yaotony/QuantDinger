"""Offline TXO option backtester (issue #67). Research only: results are not live performance.

Each trading day, in order:

1. scheduled exits close positions at the day's reference price minus/plus slippage;
2. new entries open (liquidity filter, then margin check);
3. positions whose expiry date has been reached are cash settled at the final
   settlement price (missing price -> :class:`SettlementPriceMissing`);
4. every open position is marked at the day's settlement price (missing price ->
   :class:`MissingQuoteError`), giving the equity curve and the margin in use.

No position is skipped: a leg that cannot trade (illiquid) keeps the whole
structure open, and the run reports positions still open at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.options_backtest.config import FILL_CLOSE, BacktestConfig, txo_tick_size
from app.services.options_backtest.market import OptionMarket
from app.services.options_backtest.report import build_result
from app.services.options_backtest.strategy import (
    BUY,
    SELL,
    Leg,
    LegSpec,
    RuleSpec,
    StrategySpec,
    TradePlan,
    check_strike_order,
    intrinsic,
    max_loss,
    resolve_strike,
    structure_margin,
)


@dataclass
class Fill:
    price: float
    reference: float
    cash: float  # signed premium cash flow, costs excluded
    fee: float
    tax: float
    slippage: float


@dataclass
class Position:
    id: int
    structure: str
    expiry: str
    expiry_date: date
    quantity: int
    legs: List[Leg]
    entry_date: date
    entry_prices: List[float]
    max_loss: Optional[float]
    exit_date: Optional[date] = None
    exit_days_before_expiry: int = 0
    cash_flow: float = 0.0
    costs: Dict[str, float] = field(default_factory=lambda: {"fee": 0.0, "tax": 0.0, "slippage": 0.0})


class OptionsBacktester:
    def __init__(self, market: OptionMarket, config: BacktestConfig) -> None:
        self.market = market
        self.config = config
        self.costs = config.costs
        self.multiplier = config.costs.multiplier

    # ------------------------------------------------------------------ run
    def run(self, strategy: StrategySpec, start: date, end: date) -> Dict[str, Any]:
        if end < start:
            raise ValueError("end must not be earlier than start")
        self.cash = float(self.config.initial_capital)
        self.open: List[Position] = []
        self.closed: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.curve: List[Dict[str, Any]] = []
        self.cost_totals = {"fee": 0.0, "tax": 0.0, "slippage": 0.0}
        self._next_id = 1
        self._entered_expiries: set = set()
        pending = sorted(strategy.trades, key=lambda plan: plan.entry_date)
        for day in self.market.trading_days(start, end):
            self._run_exits(day)
            pending = self._run_planned_entries(day, pending)
            if strategy.rule is not None:
                self._run_rule_entry(day, strategy.rule)
            self._settle_expired(day)
            self._mark(day)
        for plan in pending:
            self._event(plan.entry_date, "entry_skipped", "entry date is outside the backtest data", plan.expiry)
        return build_result(self, strategy, start, end)

    # ------------------------------------------------------------------ events
    def _event(self, day: date, kind: str, reason: str, expiry: str = "", position_id: Optional[int] = None) -> None:
        item = {"date": day.isoformat(), "type": kind, "reason": reason}
        if expiry:
            item["expiry"] = expiry
        if position_id is not None:
            item["position_id"] = position_id
        self.events.append(item)

    # ------------------------------------------------------------------ fills
    def _liquidity_reason(self, day: date, expiry: str, leg: Leg) -> Tuple[Optional[str], Optional[float]]:
        quote = self.market.quote(day, expiry, leg.strike, leg.right)
        label = f"{expiry} {leg.right} {leg.strike:g}"
        if quote is None:
            return f"no quote for {label}", None
        limits = self.config.liquidity
        if quote.volume < limits.min_volume or quote.open_interest < limits.min_open_interest:
            return (
                f"illiquid {label}: volume {quote.volume:g} < {limits.min_volume:g} or "
                f"open interest {quote.open_interest:g} < {limits.min_open_interest:g}"
            ), None
        reference = quote.close if self.costs.fill_price == FILL_CLOSE else quote.settlement
        if reference is None:
            return f"no {self.costs.fill_price} price for {label}", None
        if quote.settlement is None:
            return f"no settlement price for {label}; it could not be marked", None
        return None, reference

    def _fill(self, reference: float, action: str, quantity: int) -> Fill:
        """Price and costs of trading ``quantity`` contracts; ``action`` is buy or sell."""
        scale = self.costs.cost_multiplier
        slip = self.costs.slippage_ticks * scale * txo_tick_size(reference)
        price = reference + slip if action == BUY else max(0.0, reference - slip)
        amount = price * self.multiplier * quantity
        return Fill(
            price=price,
            reference=reference,
            cash=-amount if action == BUY else amount,
            fee=self.costs.fee_per_contract * quantity * scale,
            tax=amount * self.costs.tax_rate * scale,
            slippage=abs(price - reference) * self.multiplier * quantity,
        )

    def _book(self, position: Position, fills: Sequence[Fill]) -> None:
        for fill in fills:
            self.cash += fill.cash - fill.fee - fill.tax
            position.cash_flow += fill.cash - fill.fee - fill.tax
            for key, value in (("fee", fill.fee), ("tax", fill.tax), ("slippage", fill.slippage)):
                position.costs[key] += value
                self.cost_totals[key] += value

    def _price_legs(self, day: date, expiry: str, legs: Sequence[Leg], closing: bool, quantity: int):
        fills: List[Fill] = []
        for leg in legs:
            reason, reference = self._liquidity_reason(day, expiry, leg)
            if reason is not None:
                return None, reason
            # Closing reverses the leg: a long leg is sold, a short leg bought back.
            action = BUY if (leg.side == BUY) != closing else SELL
            fills.append(self._fill(reference, action, quantity))
        return fills, None

    # ------------------------------------------------------------------ valuation
    def _marks(self, day: date, position: Position) -> List[float]:
        return [self.market.mark(day, position.expiry, leg.strike, leg.right) for leg in position.legs]

    def _position_value(self, position: Position, marks: Sequence[float]) -> float:
        return sum(leg.sign * mark for leg, mark in zip(position.legs, marks)) * self.multiplier * position.quantity

    def _position_margin(self, day: date, position: Position, marks: Sequence[float]) -> float:
        return structure_margin(
            position.structure, position.legs, marks, self.market.underlying(day, position.expiry),
            self.multiplier, position.quantity, self.config.margin,
        )

    def _equity_and_margin(self, day: date) -> Tuple[float, float]:
        equity, margin = self.cash, 0.0
        for position in self.open:
            marks = self._marks(day, position)
            equity += self._position_value(position, marks)
            margin += self._position_margin(day, position, marks)
        return equity, margin

    # ------------------------------------------------------------------ entries
    def _resolve_legs(self, day: date, expiry: str, specs: Sequence[LegSpec]) -> Tuple[Optional[List[Leg]], str]:
        underlying: Optional[float] = None
        legs: List[Leg] = []
        for spec in specs:
            listed = self.market.strikes(day, expiry, spec.right)
            if spec.strike is not None:
                if spec.strike not in listed:
                    return None, f"strike {spec.strike:g} {spec.right} not listed for {expiry}"
                legs.append(Leg(spec.side, spec.right, spec.strike))
                continue
            if underlying is None:
                underlying = self.market.underlying(day, expiry)
                if underlying is None:
                    return None, f"no put-call parity underlying estimate for {expiry}"
            strike = resolve_strike(underlying + float(spec.strike_offset or 0), listed)
            if strike is None:
                return None, f"no listed {spec.right} strikes for {expiry}"
            legs.append(Leg(spec.side, spec.right, strike))
        return legs, ""

    def _try_open(self, day: date, structure: str, expiry: str, specs: Sequence[LegSpec], quantity: int,
                  exit_date: Optional[date] = None, exit_dbe: int = 0) -> bool:
        expiry_date = self.market.expiry_date(expiry)
        if day >= expiry_date:
            self._event(day, "entry_rejected", f"{expiry} expires on {expiry_date.isoformat()}", expiry)
            return False
        legs, reason = self._resolve_legs(day, expiry, specs)
        reason = reason or check_strike_order(structure, legs or []) or ""
        fills = None
        if not reason:
            fills, reason = self._price_legs(day, expiry, legs, closing=False, quantity=quantity)
        if reason:
            self._event(day, "entry_rejected", reason, expiry)
            return False
        position = Position(
            id=self._next_id, structure=structure, expiry=expiry, expiry_date=expiry_date, quantity=quantity,
            legs=list(legs), entry_date=day, entry_prices=[fill.price for fill in fills],
            max_loss=max_loss(legs, [fill.price for fill in fills], self.multiplier, quantity),
            exit_date=exit_date, exit_days_before_expiry=exit_dbe,
        )
        reason = self._margin_reason(day, position, fills)
        if reason:
            self._event(day, "entry_rejected", reason, expiry)
            return False
        self._next_id += 1
        self._book(position, fills)
        self.open.append(position)
        self._event(day, "entry", f"opened {structure}", expiry, position.id)
        return True

    def _margin_reason(self, day: date, position: Position, fills: Sequence[Fill]) -> str:
        """Reject an entry whose margin plus debit and costs exceed the free equity."""
        equity, margin_used = self._equity_and_margin(day)
        new_margin = self._position_margin(day, position, self._marks(day, position))
        debit = max(0.0, -sum(fill.cash for fill in fills))
        costs = sum(fill.fee + fill.tax for fill in fills)
        required = new_margin + debit + costs
        available = equity - margin_used
        if required > available:
            return (
                f"insufficient margin: needs {required:,.2f} (margin {new_margin:,.2f}, "
                f"debit {debit:,.2f}, costs {costs:,.2f}) but only {available:,.2f} is free"
            )
        return ""

    def _run_planned_entries(self, day: date, pending: List[TradePlan]) -> List[TradePlan]:
        remaining: List[TradePlan] = []
        for plan in pending:
            if plan.entry_date > day:
                remaining.append(plan)
                continue
            # An explicit trade is attempted once, on the first trading day on or after its date.
            self._try_open(day, plan.structure, plan.expiry, plan.legs, plan.quantity, exit_date=plan.exit_date)
        return remaining

    def _run_rule_entry(self, day: date, rule: RuleSpec) -> None:
        for expiry in self.market.listed_expiries(day):
            days_left = (self.market.expiry_date(expiry) - day).days
            if days_left < rule.min_days_to_expiry:
                continue
            # The nearest eligible expiry; retried daily until it opens or leaves the window.
            if days_left <= rule.entry_days_before_expiry and expiry not in self._entered_expiries:
                if self._try_open(day, rule.structure, expiry, rule.legs, rule.quantity,
                                  exit_dbe=rule.exit_days_before_expiry):
                    self._entered_expiries.add(expiry)
            return

    # ------------------------------------------------------------------ exits / expiry
    def _exit_due(self, day: date, position: Position) -> bool:
        if day > position.expiry_date:
            return False
        if position.exit_date is not None:
            return day >= position.exit_date
        dbe = position.exit_days_before_expiry
        return dbe > 0 and (position.expiry_date - day).days <= dbe

    def _run_exits(self, day: date) -> None:
        for position in list(self.open):
            if not self._exit_due(day, position):
                continue
            fills, reason = self._price_legs(day, position.expiry, position.legs, closing=True,
                                             quantity=position.quantity)
            if reason:
                self._event(day, "exit_blocked", reason + "; position stays open", position.expiry, position.id)
                continue
            self._book(position, fills)
            self._close(position, day, "exit", [fill.price for fill in fills])

    def _settle_expired(self, day: date) -> None:
        for position in list(self.open):
            if day < position.expiry_date:
                continue
            price = self.market.final_settlement(position.expiry)
            scale = self.costs.cost_multiplier
            exits: List[float] = []
            for leg in position.legs:
                value = intrinsic(leg.right, leg.strike, price)
                amount = value * self.multiplier * position.quantity
                tax = amount * self.costs.settlement_tax_rate * scale
                fee = self.costs.settlement_fee_per_contract * position.quantity * scale if value > 0 else 0.0
                fill = Fill(price=value, reference=value, cash=leg.sign * amount, fee=fee, tax=tax, slippage=0.0)
                self._book(position, [fill])
                exits.append(value)
            self._close(position, day, "expiry", exits, settlement_price=price)

    def _close(self, position: Position, day: date, reason: str, exit_prices: Sequence[float],
               settlement_price: Optional[float] = None) -> None:
        self.open.remove(position)
        record = trade_record(position, self.multiplier)
        record.update({
            "exit_date": day.isoformat(),
            "exit_reason": reason,
            "exit_prices": [round(p, 4) for p in exit_prices],
            "pnl": round(position.cash_flow, 4),
        })
        if settlement_price is not None:
            record["final_settlement_price"] = settlement_price
        self.closed.append(record)
        self._event(day, reason, f"closed {position.structure}", position.expiry, position.id)

    def _mark(self, day: date) -> None:
        equity, margin = self._equity_and_margin(day)
        if margin > equity:
            self._event(day, "margin_shortfall", f"margin {margin:,.2f} exceeds equity {equity:,.2f}")
        self.curve.append({
            "date": day.isoformat(),
            "equity": round(equity, 4),
            "cash": round(self.cash, 4),
            "margin": round(margin, 4),
            "open_positions": len(self.open),
        })


def trade_record(position: Position, multiplier: float) -> Dict[str, Any]:
    return {
        "id": position.id,
        "structure": position.structure,
        "expiry": position.expiry,
        "expiry_date": position.expiry_date.isoformat(),
        "quantity": position.quantity,
        "legs": [{"side": leg.side, "right": leg.right, "strike": leg.strike} for leg in position.legs],
        "entry_date": position.entry_date.isoformat(),
        "entry_prices": [round(p, 4) for p in position.entry_prices],
        "max_loss": None if position.max_loss is None else round(position.max_loss, 4),
        "max_loss_unlimited": position.max_loss is None,
        "costs": {key: round(value, 4) for key, value in position.costs.items()},
        "cash_flow": round(position.cash_flow, 4),
    }
