"""Strategy files, position structures, maximum loss and margin of the offline option backtester.

A strategy file is JSON with either explicit ``trades`` or a monthly ``rule``::

    {"name": "txo-bull-put", "config": {...},
     "rule": {"structure": "bull_put_spread", "quantity": 1,
              "legs": [{"side": "buy", "right": "put", "strike_offset": -600},
                       {"side": "sell", "right": "put", "strike_offset": -300}],
              "entry_days_before_expiry": 30, "min_days_to_expiry": 7,
              "exit_days_before_expiry": 0}}

    {"name": "one-call", "trades": [
        {"entry_date": "2026-08-03", "expiry": "202608", "structure": "long_call",
         "legs": [{"side": "buy", "right": "call", "strike": 23000}], "quantity": 1}]}

A leg has an absolute ``strike`` or a ``strike_offset`` in index points from the
underlying implied by put-call parity on the entry day (rounded to the nearest
listed strike). Supported structures: single long or short legs, the four
vertical spreads and the iron condor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.options_backtest.config import MarginConfig

BUY = "buy"
SELL = "sell"
CALL = "call"
PUT = "put"

# structure -> ordered (side, right) pattern; strike order is checked separately.
STRUCTURES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "long_call": ((BUY, CALL),),
    "long_put": ((BUY, PUT),),
    "short_call": ((SELL, CALL),),
    "short_put": ((SELL, PUT),),
    "bull_call_spread": ((BUY, CALL), (SELL, CALL)),
    "bear_call_spread": ((SELL, CALL), (BUY, CALL)),
    "bull_put_spread": ((BUY, PUT), (SELL, PUT)),
    "bear_put_spread": ((SELL, PUT), (BUY, PUT)),
    "iron_condor": ((BUY, PUT), (SELL, PUT), (SELL, CALL), (BUY, CALL)),
}
CREDIT_SPREADS = {"bear_call_spread", "bull_put_spread"}
SHORT_SINGLES = {"short_call", "short_put"}


@dataclass(frozen=True)
class LegSpec:
    side: str
    right: str
    strike: Optional[float] = None
    strike_offset: Optional[float] = None


@dataclass(frozen=True)
class Leg:
    side: str
    right: str
    strike: float

    @property
    def sign(self) -> int:
        return 1 if self.side == BUY else -1


@dataclass(frozen=True)
class TradePlan:
    structure: str
    legs: Tuple[LegSpec, ...]
    quantity: int
    entry_date: date
    expiry: str
    exit_date: Optional[date] = None


@dataclass(frozen=True)
class RuleSpec:
    structure: str
    legs: Tuple[LegSpec, ...]
    quantity: int
    entry_days_before_expiry: int = 30
    min_days_to_expiry: int = 7
    exit_days_before_expiry: int = 0


@dataclass(frozen=True)
class StrategySpec:
    name: str
    trades: Tuple[TradePlan, ...] = ()
    rule: Optional[RuleSpec] = None
    config: Mapping[str, Any] = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- parsing
def _leg_spec(raw: Mapping[str, Any]) -> LegSpec:
    side = str(raw.get("side") or "").strip().lower()
    right = str(raw.get("right") or "").strip().lower()
    if side not in (BUY, SELL) or right not in (CALL, PUT):
        raise ValueError(f"leg needs side buy/sell and right call/put: {dict(raw)}")
    strike = raw.get("strike")
    offset = raw.get("strike_offset")
    if (strike is None) == (offset is None):
        raise ValueError("leg needs exactly one of strike or strike_offset")
    return LegSpec(side, right, None if strike is None else float(strike), None if offset is None else float(offset))


def _legs(structure: str, raw_legs: Any) -> Tuple[LegSpec, ...]:
    if structure not in STRUCTURES:
        raise ValueError(f"unsupported structure: {structure}; choose one of {', '.join(STRUCTURES)}")
    legs = tuple(_leg_spec(item) for item in (raw_legs or []))
    pattern = tuple((leg.side, leg.right) for leg in legs)
    if pattern != STRUCTURES[structure]:
        expected = ", ".join(f"{side} {right}" for side, right in STRUCTURES[structure])
        raise ValueError(f"{structure} legs must be, in strike order: {expected}")
    return legs


def _quantity(value: Any) -> int:
    quantity = int(value if value is not None else 1)
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    return quantity


def _expiry(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) != 6 or not text.isdigit() or not 1 <= int(text[4:]) <= 12:
        raise ValueError(f"expiry must be a monthly contract month like 202608, got {value!r}")
    return text


def _trade(raw: Mapping[str, Any]) -> TradePlan:
    structure = str(raw.get("structure") or "")
    exit_date = raw.get("exit_date")
    return TradePlan(
        structure=structure,
        legs=_legs(structure, raw.get("legs")),
        quantity=_quantity(raw.get("quantity")),
        entry_date=date.fromisoformat(str(raw["entry_date"])),
        expiry=_expiry(raw.get("expiry")),
        exit_date=date.fromisoformat(str(exit_date)) if exit_date else None,
    )


def _rule(raw: Mapping[str, Any]) -> RuleSpec:
    structure = str(raw.get("structure") or "")
    rule = RuleSpec(
        structure=structure,
        legs=_legs(structure, raw.get("legs")),
        quantity=_quantity(raw.get("quantity")),
        entry_days_before_expiry=int(raw.get("entry_days_before_expiry", 30)),
        min_days_to_expiry=int(raw.get("min_days_to_expiry", 7)),
        exit_days_before_expiry=int(raw.get("exit_days_before_expiry", 0)),
    )
    if rule.min_days_to_expiry > rule.entry_days_before_expiry:
        raise ValueError("min_days_to_expiry must not exceed entry_days_before_expiry")
    if not 0 <= rule.exit_days_before_expiry < rule.min_days_to_expiry:
        raise ValueError("exit_days_before_expiry must be >= 0 and below min_days_to_expiry")
    return rule


def parse_strategy(raw: Mapping[str, Any]) -> StrategySpec:
    if not isinstance(raw, Mapping):
        raise ValueError("strategy file must be a JSON object")
    has_trades, has_rule = bool(raw.get("trades")), bool(raw.get("rule"))
    if has_trades == has_rule:
        raise ValueError("strategy needs exactly one of 'trades' or 'rule'")
    return StrategySpec(
        name=str(raw.get("name") or "options-strategy"),
        trades=tuple(_trade(item) for item in raw.get("trades") or []),
        rule=_rule(raw["rule"]) if has_rule else None,
        config=dict(raw.get("config") or {}),
    )


# --------------------------------------------------------------------------- structure checks
def check_strike_order(structure: str, legs: Sequence[Leg]) -> Optional[str]:
    """None when the resolved strikes form ``structure``; otherwise the reason."""
    strikes = [leg.strike for leg in legs]
    if structure == "iron_condor":
        put_low, put_high, call_low, call_high = strikes
        ok = put_low < put_high <= call_low < call_high
    else:
        ok = all(a < b for a, b in zip(strikes, strikes[1:]))
    return None if ok else f"strikes {strikes} do not form a {structure}"


def intrinsic(right: str, strike: float, underlying: float) -> float:
    return max(underlying - strike, 0.0) if right == CALL else max(strike - underlying, 0.0)


def payoff_points(legs: Sequence[Leg], underlying: float) -> float:
    """Expiry value of one unit of the structure in index points (long positive)."""
    return sum(leg.sign * intrinsic(leg.right, leg.strike, underlying) for leg in legs)


def max_loss(legs: Sequence[Leg], entry_prices: Sequence[float], multiplier: float, quantity: int) -> Optional[float]:
    """Largest expiry loss in NT$ from the entry premiums (costs excluded); None if unlimited.

    The payoff is piecewise linear with kinks at the strikes, so its minimum is at
    0, a strike, or at infinity (unlimited when net short calls).
    """
    net_calls = sum(leg.sign for leg in legs if leg.right == CALL)
    if net_calls < 0:
        return None
    premium = sum(leg.sign * price for leg, price in zip(legs, entry_prices))
    worst = min(payoff_points(legs, s) for s in [0.0, *[leg.strike for leg in legs]])
    return max(0.0, premium - worst) * multiplier * quantity


def structure_margin(
    structure: str,
    legs: Sequence[Leg],
    marks: Sequence[float],
    underlying: Optional[float],
    multiplier: float,
    quantity: int,
    margin: MarginConfig,
) -> float:
    """Approximate TAIFEX margin of one position in NT$ (see the backtest guide).

    * long legs and debit spreads: 0 (the premium is paid in full)
    * short single: premium market value + max(A - out-of-money amount, B);
      without an underlying estimate the out-of-money amount is taken as 0
    * credit vertical spread: strike width x multiplier
    * iron condor: the wider of the two spreads x multiplier
    """
    if structure in SHORT_SINGLES:
        leg, mark = legs[0], marks[0]
        otm = 0.0
        if underlying is not None:
            otm = max(leg.strike - underlying, 0.0) if leg.right == CALL else max(underlying - leg.strike, 0.0)
        per_unit = mark * multiplier + max(margin.margin_a - otm * multiplier, margin.margin_b)
        return per_unit * quantity
    if structure in CREDIT_SPREADS:
        return abs(legs[1].strike - legs[0].strike) * multiplier * quantity
    if structure == "iron_condor":
        width = max(legs[1].strike - legs[0].strike, legs[3].strike - legs[2].strike)
        return width * multiplier * quantity
    return 0.0


def resolve_strike(target: float, listed: List[float]) -> Optional[float]:
    """Nearest listed strike to ``target`` (the lower one on a tie)."""
    if not listed:
        return None
    return min(listed, key=lambda strike: (abs(strike - target), strike))
