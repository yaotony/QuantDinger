"""Cost, liquidity and margin parameters of the offline option backtester.

Every default is a research assumption and is listed, with its source and what
the owner must confirm, in ``docs/trading/TAIFEX_OPTIONS_BACKTEST.md``. Fees and
margins change over time and differ between brokers; override them per run.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Mapping

FILL_CLOSE = "close"
FILL_SETTLEMENT = "settlement"


@dataclass(frozen=True)
class CostConfig:
    # TXO contract multiplier: NT$50 per index point.
    multiplier: float = 50.0
    # Futures transaction tax on option premium: 0.1% of the premium amount, per side.
    tax_rate: float = 0.001
    # Tax on the cash-settled value of an in-the-money option at expiry.
    settlement_tax_rate: float = 0.001
    # Broker commission (including exchange / clearing fees) per contract per side, NT$.
    fee_per_contract: float = 25.0
    # Broker fee per contract settled in the money at expiry, NT$.
    settlement_fee_per_contract: float = 25.0
    # Slippage in price ticks added to a buy and subtracted from a sell.
    slippage_ticks: float = 1.0
    # Research rule: scale every cost (fees, taxes, slippage) by this factor (2.0 = double cost).
    cost_multiplier: float = 1.0
    # Reference price of a fill: the day's close or settlement price.
    fill_price: str = FILL_CLOSE

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        if self.cost_multiplier <= 0:
            raise ValueError("cost_multiplier must be positive")
        for name in ("tax_rate", "settlement_tax_rate", "fee_per_contract",
                     "settlement_fee_per_contract", "slippage_ticks"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.fill_price not in (FILL_CLOSE, FILL_SETTLEMENT):
            raise ValueError("fill_price must be 'close' or 'settlement'")


@dataclass(frozen=True)
class LiquidityConfig:
    # A strike trades only when the day's volume and open interest reach both thresholds.
    min_volume: float = 20.0
    min_open_interest: float = 100.0


@dataclass(frozen=True)
class MarginConfig:
    # TAIFEX TXO short-option margin: premium market value + max(A - out-of-money amount, B).
    # A and B are NT$ amounts TAIFEX revises from time to time; set them for the tested period.
    margin_a: float = 85_000.0
    margin_b: float = 43_000.0


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    product: str = "TXO"
    costs: CostConfig = field(default_factory=CostConfig)
    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    margin: MarginConfig = field(default_factory=MarginConfig)

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if str(self.product).upper() != "TXO":
            raise ValueError("only TXO monthly options are supported")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _section(cls, raw: Any):
    if raw is None:
        return cls()
    if not isinstance(raw, Mapping):
        raise ValueError(f"{cls.__name__} must be an object")
    known = {item.name for item in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {', '.join(unknown)}")
    values = {key: (value if key == "fill_price" else float(value)) for key, value in raw.items()}
    return cls(**values)


def config_from_mapping(raw: Mapping[str, Any], *, cost_multiplier: Any = None) -> BacktestConfig:
    """Build a config from the strategy file's ``config`` object; ``cost_multiplier`` overrides it."""
    raw = dict(raw or {})
    costs = dict(raw.get("costs") or {})
    if cost_multiplier is not None:
        costs["cost_multiplier"] = cost_multiplier
    return BacktestConfig(
        initial_capital=float(raw.get("initial_capital", BacktestConfig.initial_capital)),
        product=str(raw.get("product", "TXO")),
        costs=_section(CostConfig, costs),
        liquidity=_section(LiquidityConfig, raw.get("liquidity")),
        margin=_section(MarginConfig, raw.get("margin")),
    )


def txo_tick_size(price: float) -> float:
    """TXO minimum price fluctuation for a premium level (points)."""
    value = abs(float(price))
    if value < 10:
        return 0.1
    if value < 50:
        return 0.5
    if value < 500:
        return 1.0
    if value < 1000:
        return 5.0
    return 10.0
