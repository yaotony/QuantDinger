"""Pure helpers that turn Fubon (Fugle) market data payloads into the sidecar v1 shape.

Every timestamp leaves the sidecar as unix seconds (UTC). Fubon's candle rows carry the
bar time in ``date`` (an ISO string such as ``2026-07-06T08:45:00.000+08:00``, or a bare
``YYYY-MM-DD`` for daily and longer bars), not in ``time`` as the documentation says.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

TAIPEI = timezone(timedelta(hours=8))

_SECONDS_UPPER_BOUND = 100_000_000_000  # ~year 5138 in seconds; anything larger is ms or us
_MILLIS_UPPER_BOUND = 100_000_000_000_000


def epoch_number_to_seconds(value: float) -> int:
    """Convert an epoch number in seconds, milliseconds or microseconds to seconds."""
    number = float(value)
    if abs(number) >= _MILLIS_UPPER_BOUND:
        return int(number // 1_000_000)
    if abs(number) >= _SECONDS_UPPER_BOUND:
        return int(number // 1_000)
    return int(number)


def to_unix_seconds(value: Any) -> int | None:
    """Convert a Fubon time value (ISO string, date string or epoch number) to unix seconds.

    Date-only strings and naive datetimes are interpreted in Asia/Taipei, the exchange
    time zone, so a daily bar dated ``2026-07-06`` opens at 2026-07-06T00:00+08:00.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return epoch_number_to_seconds(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=TAIPEI)
        return int(moment.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=TAIPEI).timestamp())
    text = str(value).strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return epoch_number_to_seconds(int(text))
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI).timestamp())
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TAIPEI)
    return int(moment.timestamp())


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def data_rows(payload: Any) -> list[dict[str, Any]]:
    """Return the ``data`` list of a Fubon REST payload, tolerating missing keys."""
    if isinstance(payload, dict):
        rows = payload.get("data")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def normalize_candles(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize candle rows to ``{time, open, high, low, close, volume}`` sorted ascending.

    Rows without a parseable time or OHLC value are dropped; duplicate times keep the
    last row Fubon returned.
    """
    by_time: dict[int, dict[str, Any]] = {}
    for row in rows:
        moment = to_unix_seconds(row.get("date", row.get("time")))
        if moment is None:
            continue
        ohlc = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
        if any(value is None for value in ohlc):
            continue
        volume = _number(row.get("volume"))
        by_time[moment] = {
            "time": moment,
            "open": ohlc[0],
            "high": ohlc[1],
            "low": ohlc[2],
            "close": ohlc[3],
            "volume": volume if volume is not None else 0.0,
        }
    return [by_time[moment] for moment in sorted(by_time)]


def normalize_quote(payload: Any, symbol: str) -> dict[str, Any]:
    """Pick the stable quote fields shared by stock and futures/options quotes."""
    quote = payload if isinstance(payload, dict) else {}
    raw_total = quote.get("total")
    total: dict[str, Any] = raw_total if isinstance(raw_total, dict) else {}
    last_price = _number(quote.get("lastPrice"))
    if last_price is None:
        last_price = _number(quote.get("closePrice"))
    updated = quote.get("lastUpdated")
    if updated is None:
        updated = quote.get("closeTime")
    return {
        "symbol": str(quote.get("symbol") or symbol),
        "name": quote.get("name"),
        "lastPrice": last_price,
        "change": _number(quote.get("change")),
        "changePercent": _number(quote.get("changePercent")),
        "open": _number(quote.get("openPrice")),
        "high": _number(quote.get("highPrice")),
        "low": _number(quote.get("lowPrice")),
        "previousClose": _number(quote.get("previousClose")),
        "referencePrice": _number(quote.get("referencePrice")),
        "volume": _number(total.get("tradeVolume")),
        "time": to_unix_seconds(updated),
    }


def has_quote(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("symbol") or payload.get("name"))


STOCK_TICKER_FIELDS = ("symbol", "name")
FUTOPT_TICKER_FIELDS = (
    "symbol",
    "name",
    "referencePrice",
    "contractType",
    "startDate",
    "endDate",
    "settlementDate",
)


def normalize_tickers(rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only whitelisted ticker fields; contract dates stay ``YYYY-MM-DD`` strings."""
    tickers = []
    for row in rows:
        if not row.get("symbol"):
            continue
        tickers.append({key: row.get(key) for key in fields})
    return tickers
