"""TAIFEX option chain: call and put quotes per strike for one product and expiry month.

Read-only market data from the Fubon sidecar: the ``/v1/futopt/tickers?type=OPTION``
list picks the series and ``/v1/futopt/quote`` prices each contract. The sidecar
caps Fubon requests per second, so a chain request quotes at most
``MAX_STRIKES`` strikes (two contracts each), centred on the underlying price.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.data_sources.tw_futures_contracts import TAIPEI_TZ, is_expired
from app.data_sources.tw_options import CALL, PUT, OptionContract, option_product, parse_option_symbol
from app.data_sources.tw_stock import FubonSidecarError, TWStockDataSource
from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_STRIKES = 10
MAX_STRIKES = 25
QUOTE_WORKERS = 4
TICKERS_TTL_SECONDS = 300


class OptionChainError(ValueError):
    """Invalid chain request (missing product, malformed month)."""


class OptionChainNotFound(OptionChainError):
    """No listed (or allowed) series for the requested product and month."""


@dataclass(frozen=True)
class _Series:
    contract: OptionContract
    expiry: Optional[date]


def normalize_expiry_month(value: Any) -> Optional[Tuple[int, int]]:
    """``2026-08`` / ``202608`` -> ``(2026, 8)``; None when empty; raise when malformed."""
    raw = str(value or "").strip().replace("/", "-")
    if not raw:
        return None
    digits = raw.replace("-", "")
    if len(digits) != 6 or not digits.isdigit():
        raise OptionChainError("expiry_month must be YYYY-MM")
    year, month = int(digits[:4]), int(digits[4:])
    if not 1 <= month <= 12:
        raise OptionChainError("expiry_month must be YYYY-MM")
    return year, month


def _ticker_expiry(row: Dict[str, Any]) -> Optional[date]:
    for key in ("endDate", "settlementDate"):
        value = row.get(key)
        if not value:
            continue
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def _num(payload: Dict[str, Any], key: str) -> Optional[float]:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_view(symbol: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not payload:
        return {"symbol": symbol, "available": False}
    return {
        "symbol": symbol,
        "available": True,
        "name": str(payload.get("name") or ""),
        "lastPrice": _num(payload, "lastPrice"),
        "change": _num(payload, "change"),
        "changePercent": _num(payload, "changePercent"),
        "open": _num(payload, "open"),
        "high": _num(payload, "high"),
        "low": _num(payload, "low"),
        "previousClose": _num(payload, "previousClose"),
        "referencePrice": _num(payload, "referencePrice"),
        "volume": _num(payload, "volume"),
        "time": payload.get("time"),
    }


class TWOptionChainService:
    """Build an option chain from the sidecar ticker list and per-contract quotes."""

    def __init__(
        self,
        source: Any = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        quote_workers: int = QUOTE_WORKERS,
        tickers_ttl: float = TICKERS_TTL_SECONDS,
    ) -> None:
        self.source = source or TWStockDataSource()
        self._clock = clock or time.time
        self._quote_workers = max(1, int(quote_workers))
        self._tickers_ttl = float(tickers_ttl)
        self._tickers: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._tickers_lock = threading.Lock()

    # ------------------------------------------------------------------ inputs
    def _option_tickers(self) -> List[Dict[str, Any]]:
        """The sidecar OPTION list, cached for ``tickers_ttl`` seconds (thousands of rows)."""
        now = self._clock()
        with self._tickers_lock:
            if self._tickers is not None and now - self._tickers[0] < self._tickers_ttl:
                return self._tickers[1]
        rows = self.source.get_futopt_tickers("OPTION")
        with self._tickers_lock:
            self._tickers = (now, rows)
        return rows

    def _quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            return self.source._request("/v1/futopt/quote", {"symbol": symbol})
        except FubonSidecarError as exc:
            logger.info("TW option chain: quote unavailable for %s: %s", symbol, exc)
            return None

    def _underlying(self, product: str) -> Dict[str, Any]:
        spec = option_product(product)
        if spec is None:
            return {"symbol": None, "price": None}
        symbol = f"TWStock:{spec.underlying}"
        try:
            payload = self.source._request("/v1/stock/quote", {"symbol": spec.underlying})
        except FubonSidecarError as exc:
            logger.info("TW option chain: underlying quote unavailable for %s: %s", symbol, exc)
            payload = None
        price = _num(payload or {}, "lastPrice") or _num(payload or {}, "previousClose")
        return {"symbol": symbol, "price": price}

    # ------------------------------------------------------------------ chain
    def get_chain(
        self,
        product: str,
        expiry_month: Any = None,
        *,
        strikes: int = DEFAULT_STRIKES,
        center: Optional[float] = None,
        symbol_allowed: Optional[Callable[[str], bool]] = None,
    ) -> Dict[str, Any]:
        """Quotes per strike for ``product`` (``TXO``, ``TX1``, ...) and ``expiry_month``.

        Without ``expiry_month`` the nearest unexpired series is used. ``strikes``
        (1..``MAX_STRIKES``) strikes nearest to ``center`` (default: the underlying
        price, else the middle of the listed strikes) are quoted. ``symbol_allowed``
        filters contracts by the caller's instrument allowlist.
        """
        code = str(product or "").strip().upper()
        if not code:
            raise OptionChainError("product is required")
        month = normalize_expiry_month(expiry_month)
        count = max(1, min(MAX_STRIKES, int(strikes or DEFAULT_STRIKES)))
        now = datetime.fromtimestamp(self._clock(), tz=TAIPEI_TZ)

        series: List[_Series] = []
        for row in self._option_tickers():
            contract = parse_option_symbol(row.get("symbol"), now.date())
            if contract is None or contract.product != code:
                continue
            expiry = _ticker_expiry(row) or contract.expiry
            if month is None and expiry is not None and is_expired(expiry, now):
                continue
            if symbol_allowed is not None and not (symbol_allowed(contract.symbol) or symbol_allowed(code)):
                continue
            series.append(_Series(contract, expiry))
        if not series:
            raise OptionChainNotFound(f"no listed option series for {code}")

        if month is None:
            months = sorted(
                {(s.expiry or date.max, s.contract.year, s.contract.month) for s in series}
            )
            month = (months[0][1], months[0][2])
        chosen = [s for s in series if (s.contract.year, s.contract.month) == month]
        if not chosen:
            raise OptionChainNotFound(f"no listed {code} series for {month[0]:04d}-{month[1]:02d}")

        by_strike: Dict[int, Dict[str, _Series]] = {}
        for item in chosen:
            by_strike.setdefault(item.contract.strike, {})[item.contract.right] = item
        all_strikes = sorted(by_strike)

        underlying = self._underlying(code)
        anchor = center if center is not None else underlying.get("price")
        if anchor is None:
            anchor = all_strikes[len(all_strikes) // 2]
        picked = sorted(sorted(all_strikes, key=lambda k: (abs(k - float(anchor)), k))[:count])

        symbols = [
            by_strike[strike][right].contract.symbol
            for strike in picked
            for right in (CALL, PUT)
            if right in by_strike[strike]
        ]
        quotes = self._quotes(symbols)

        rows = []
        for strike in picked:
            row: Dict[str, Any] = {"strike": strike}
            for right in (CALL, PUT):
                item = by_strike[strike].get(right)
                row[right] = _quote_view(item.contract.symbol, quotes.get(item.contract.symbol)) if item else None
            rows.append(row)

        expiries = {s.expiry for s in chosen if s.expiry is not None}
        spec = option_product(code)
        return {
            "market": "TWFutures",
            "product": code,
            "productName": spec.name if spec else code,
            "expiryMonth": f"{month[0]:04d}-{month[1]:02d}",
            "expiryDate": min(expiries).isoformat() if expiries else None,
            "multiplier": spec.multiplier if spec else None,
            "underlying": underlying,
            "center": float(anchor),
            "strikeCount": len(all_strikes),
            "returnedStrikes": len(picked),
            "truncated": len(picked) < len(all_strikes),
            "backtestSupported": False,
            "asOf": int(self._clock()),
            "strikes": rows,
        }

    def _quotes(self, symbols: Sequence[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        if not symbols:
            return {}
        workers = min(self._quote_workers, len(symbols))
        if workers <= 1:
            return {symbol: self._quote(symbol) for symbol in symbols}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tw-option-quote") as pool:
            results = list(pool.map(self._quote, symbols))
        return dict(zip(symbols, results))


_service: Optional[TWOptionChainService] = None
_service_lock = threading.Lock()


def get_tw_option_chain_service() -> TWOptionChainService:
    global _service
    with _service_lock:
        if _service is None:
            _service = TWOptionChainService()
        return _service
