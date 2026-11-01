"""Perpetual-swap funding simulation settings for Strategy API V2 backtests.

Funding is off by default (``not_modeled``). With ``historical_rates`` the
backtest loads stored exchange settlements (backfilling missing windows),
and the simulation broker charges or credits each open perpetual position at
every settlement it holds through:

* payment = signed position quantity * settlement-bar close * funding rate;
* a positive payment is paid (long with positive rate), a negative one is
  received (short with positive rate, or long with negative rate);
* the settlement bar is the driving bar whose span ``(open, open + interval]``
  contains the settlement timestamp, so its close approximates the mark price.

Missing settlements count as zero; the run reports every missing period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

import pandas as pd

from app.services.funding_rates import (
    BINANCE_USDM_SOURCE,
    FundingRateUnsupportedError,
    binance_usdm_symbol,
)
from app.services.market_context import default_crypto_exchange_id, normalize_exchange_id

from .contract import StrategyV2ContractError


FUNDING_MODE_NOT_MODELED = "not_modeled"
FUNDING_MODE_HISTORICAL = "historical_rates"
FUNDING_MODES = (FUNDING_MODE_NOT_MODELED, FUNDING_MODE_HISTORICAL)
FUNDING_PRICE_BASIS = "settlement_bar_close"


def normalize_funding_mode(value: Any) -> str:
    """Return a supported funding mode; empty values keep the default."""
    raw = str(value or "").strip().lower()
    if not raw:
        return FUNDING_MODE_NOT_MODELED
    if raw not in FUNDING_MODES:
        raise StrategyV2ContractError(f"strategyV2.fundingModeUnsupported:{raw}")
    return raw


@dataclass
class FundingPlan:
    """Funding schedule for the runner plus the report attached to results."""

    rates: dict[str, list[tuple[pd.Timestamp, float]]] = field(default_factory=dict)
    instruments: list[dict[str, Any]] = field(default_factory=list)

    def report(self, mode: str) -> dict[str, Any]:
        missing_periods = sum(len(item.get("missingPeriods") or []) for item in self.instruments)
        known_counts = [
            int(period["missingCount"])
            for item in self.instruments
            for period in item.get("missingPeriods") or []
            if period.get("missingCount") is not None
        ]
        warnings: list[str] = []
        for item in self.instruments:
            status = item.get("status")
            instrument = item.get("instrument")
            if status == "modeled" and item.get("missingPeriods"):
                warnings.append(f"strategyV2.fundingDataMissing:{instrument}")
            elif status in {"unsupported_exchange", "unsupported_product"}:
                warnings.append(f"strategyV2.fundingNotModeled:{instrument}:{status}")
            if item.get("fetchErrors"):
                warnings.append(f"strategyV2.fundingBackfillFailed:{instrument}")
        return {
            "mode": mode,
            "source": BINANCE_USDM_SOURCE,
            "priceBasis": FUNDING_PRICE_BASIS,
            "missingDataPolicy": "zero_rate",
            "instruments": self.instruments,
            "modeledInstruments": sum(1 for item in self.instruments if item.get("status") == "modeled"),
            "missingPeriodCount": missing_periods,
            "missingSettlementCount": sum(known_counts),
            "missingSettlementCountComplete": len(known_counts) == missing_periods,
            "warnings": warnings,
        }


def build_funding_plan(
    candidates: Iterable[Mapping[str, Any]],
    *,
    frame_keys: Iterable[str],
    start: datetime,
    end: datetime,
    provider: Any,
) -> FundingPlan:
    """Resolve funding history for every perpetual instrument in the run."""
    plan = FundingPlan()
    available = set(frame_keys)
    for member in candidates:
        key = str(member.get("key") or "")
        if not key or key not in available:
            continue
        market_type = str(member.get("market_type") or "").strip().lower()
        if str(member.get("market") or "") != "Crypto" or market_type != "swap":
            plan.instruments.append({"instrument": key, "status": "not_applicable"})
            continue
        exchange_id = normalize_exchange_id(member.get("exchange_id")) or default_crypto_exchange_id()
        entry: dict[str, Any] = {
            "instrument": key,
            "exchangeId": exchange_id,
            "status": "modeled",
        }
        if not provider.supports(exchange_id):
            entry["status"] = "unsupported_exchange"
            plan.instruments.append(entry)
            continue
        try:
            venue_symbol = binance_usdm_symbol(str(member.get("symbol") or ""))
        except FundingRateUnsupportedError:
            entry["status"] = "unsupported_product"
            plan.instruments.append(entry)
            continue
        history = provider.history(
            exchange_id=exchange_id,
            symbol=venue_symbol,
            start=start,
            end=end,
        )
        rates = [(pd.Timestamp(item.funding_time), float(item.rate)) for item in history.records]
        plan.rates[key] = rates
        entry.update({
            "venueSymbol": venue_symbol,
            "records": len(rates),
            "firstSettlement": _iso(rates[0][0]) if rates else None,
            "lastSettlement": _iso(rates[-1][0]) if rates else None,
            "backfilledRecords": int(history.fetched),
            "missingPeriods": list(history.missing_periods),
            "missingSettlementCount": sum(
                int(item["missingCount"])
                for item in history.missing_periods
                if item.get("missingCount") is not None
            ),
        })
        if history.fetch_errors:
            entry["fetchErrors"] = list(history.fetch_errors)
        plan.instruments.append(entry)
    return plan


def _iso(value: pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").isoformat().replace("+00:00", "Z")
