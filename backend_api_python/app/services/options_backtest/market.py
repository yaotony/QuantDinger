"""Daily TXO option quotes for the offline backtester.

Only regular-session daily rows are used: a close (may be missing on a day
without trades), a settlement price, volume and open interest. There is no bid
or ask in the daily data.

Missing data is an error, never a zero: a position that cannot be marked
(:class:`MissingQuoteError`) or settled (:class:`SettlementPriceMissing`) stops
the run with the series named in the message.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from app.data_sources.tw_futures_contracts import TradingDayCheck
from app.data_sources.tw_options import option_expiry_date

QuoteKey = Tuple[date, str, float, str]


class OptionsBacktestError(RuntimeError):
    """Base class of the errors that stop an option backtest."""


class SettlementPriceMissing(OptionsBacktestError):
    """An expiring position has no final settlement price."""


class MissingQuoteError(OptionsBacktestError):
    """An open position has no settlement price to be marked on a trading day."""


@dataclass(frozen=True)
class OptionQuote:
    trade_date: date
    expiry: str  # 202611 (monthly only)
    strike: float
    right: str  # call / put
    close: Optional[float]
    settlement: Optional[float]
    volume: float
    open_interest: float

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "OptionQuote":
        day = row["trade_date"]
        return cls(
            trade_date=day if isinstance(day, date) else date.fromisoformat(str(day)[:10]),
            expiry=str(row["expiry"]).strip(),
            strike=float(row["strike"]),
            right=str(row["right"]).strip().lower(),
            close=_opt_float(row.get("close")),
            settlement=_opt_float(row.get("settlement")),
            volume=float(row.get("volume") or 0),
            open_interest=float(row.get("open_interest") or 0),
        )


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


class OptionMarket:
    """In-memory daily chain of one product with final settlement prices.

    ``expiry_dates`` overrides the calendar (``{"202608": date(2026, 8, 19)}``);
    otherwise the TAIFEX last trading day comes from #59's ``option_expiry_date``.
    Weekly series (``202611W1``) are ignored: the first version covers TXO monthly only.
    """

    def __init__(
        self,
        quotes: Iterable[Any],
        final_settlements: Mapping[str, Any],
        *,
        product: str = "TXO",
        expiry_dates: Optional[Mapping[str, date]] = None,
        is_trading_day: Optional[TradingDayCheck] = None,
    ) -> None:
        self.product = product
        self._quotes: Dict[QuoteKey, OptionQuote] = {}
        self._chains: Dict[Tuple[date, str], List[OptionQuote]] = {}
        for raw in quotes:
            quote = raw if isinstance(raw, OptionQuote) else OptionQuote.from_mapping(raw)
            if len(quote.expiry) != 6:
                continue
            self._quotes[(quote.trade_date, quote.expiry, quote.strike, quote.right)] = quote
            self._chains.setdefault((quote.trade_date, quote.expiry), []).append(quote)
        self._days = sorted({key[0] for key in self._chains})
        self._settlements = {str(k): _settlement_price(v) for k, v in dict(final_settlements or {}).items()}
        self._expiry_dates = dict(expiry_dates or {})
        self._is_trading_day: Optional[Callable[[date], bool]] = is_trading_day

    # ------------------------------------------------------------------ calendar
    def trading_days(self, start: date, end: date) -> List[date]:
        return [day for day in self._days if start <= day <= end]

    def expiry_date(self, expiry: str) -> date:
        if expiry not in self._expiry_dates:
            day = option_expiry_date(self.product, int(expiry[:4]), int(expiry[4:6]), self._is_trading_day)
            if day is None:
                raise OptionsBacktestError(f"no expiry date for {self.product} {expiry}")
            self._expiry_dates[expiry] = day
        return self._expiry_dates[expiry]

    def listed_expiries(self, day: date) -> List[str]:
        return sorted(expiry for (d, expiry) in self._chains if d == day)

    # ------------------------------------------------------------------ quotes
    def quote(self, day: date, expiry: str, strike: float, right: str) -> Optional[OptionQuote]:
        return self._quotes.get((day, expiry, float(strike), right))

    def strikes(self, day: date, expiry: str, right: str) -> List[float]:
        return sorted({q.strike for q in self._chains.get((day, expiry), []) if q.right == right})

    def mark(self, day: date, expiry: str, strike: float, right: str) -> float:
        quote = self.quote(day, expiry, strike, right)
        if quote is None or quote.settlement is None:
            raise MissingQuoteError(
                f"no settlement price on {day.isoformat()} for {self.product} {expiry} {right} {strike:g}"
            )
        return quote.settlement

    def final_settlement(self, expiry: str) -> float:
        price = self._settlements.get(expiry)
        if price is None:
            raise SettlementPriceMissing(
                f"final settlement price missing for {self.product} {expiry}; "
                "import it before backtesting through this expiry"
            )
        return price

    def underlying(self, day: date, expiry: str) -> Optional[float]:
        """Implied underlying from put-call parity: K + C - P at the strike where |C - P| is smallest.

        Uses settlement prices and ignores interest (one month or less to expiry).
        """
        calls = {q.strike: q.settlement for q in self._chains.get((day, expiry), [])
                 if q.right == "call" and q.settlement is not None}
        puts = {q.strike: q.settlement for q in self._chains.get((day, expiry), [])
                if q.right == "put" and q.settlement is not None}
        pairs = [(abs(calls[k] - puts[k]), k, calls[k] - puts[k]) for k in calls.keys() & puts.keys()]
        if not pairs:
            return None
        _, strike, diff = min(pairs)
        return strike + diff


def _settlement_price(value: Any) -> Optional[float]:
    if isinstance(value, (tuple, list)):
        value = value[-1]
    if value is None:
        return None
    price = float(value)
    return price if price > 0 else None
