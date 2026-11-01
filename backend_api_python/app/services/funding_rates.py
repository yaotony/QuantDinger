"""Historical perpetual-swap funding rates for backtest funding simulation.

PostgreSQL (``qd_perpetual_funding_rates``) is the durable source. A backtest
first reads the stored settlements for its range, backfills the windows that
are missing from the exchange, persists them, and then reads the merged set.

Only Binance USD-M perpetuals (``/fapi/v1/fundingRate``) are supported. The
settlement cadence is taken from the returned timestamps; nothing assumes an
eight-hour interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Protocol

import pandas as pd

from app.utils.logger import get_logger


logger = get_logger(__name__)

BINANCE_EXCHANGE_ID = "binance"
BINANCE_USDM_SOURCE = "binance_usdm_funding_rate"
BINANCE_USDM_QUOTES = ("USDT", "USDC")
SUPPORTED_FUNDING_EXCHANGES = (BINANCE_EXCHANGE_ID,)

# A gap counts as missing data when it exceeds the local cadence by this factor.
_GAP_TOLERANCE = 1.5


@dataclass(frozen=True)
class FundingRateRecord:
    exchange_id: str
    symbol: str
    funding_time: pd.Timestamp
    rate: float
    mark_price: float | None = None


@dataclass
class FundingHistory:
    """Funding settlements for one venue symbol and their coverage report."""

    exchange_id: str
    symbol: str
    records: list[FundingRateRecord] = field(default_factory=list)
    missing_periods: list[dict[str, Any]] = field(default_factory=list)
    fetched: int = 0
    fetch_errors: list[str] = field(default_factory=list)


class FundingRateUnsupportedError(ValueError):
    """Raised when a venue or product has no supported funding-rate source."""


def utc_naive(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp


def binance_usdm_symbol(symbol: str) -> str:
    """Map a canonical ``BASE/QUOTE`` pair to its Binance USD-M symbol."""
    raw = str(symbol or "").strip().upper()
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    if "/" in raw:
        base, quote = raw.split("/", 1)
    else:
        quote = next((item for item in BINANCE_USDM_QUOTES if raw.endswith(item)), "")
        base = raw[: -len(quote)] if quote else raw
    if not base or quote not in BINANCE_USDM_QUOTES:
        raise FundingRateUnsupportedError(f"strategyV2.fundingProductUnsupported:{symbol}")
    return f"{base}{quote}"


class FundingRateClient(Protocol):
    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[FundingRateRecord]:
        ...


class BinanceUsdmFundingRateClient:
    """Paginated reader for Binance ``GET /fapi/v1/fundingRate``."""

    BASE_URL = "https://fapi.binance.com"
    PATH = "/fapi/v1/fundingRate"
    PAGE_LIMIT = 1000

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] | None = None,
        base_url: str = "",
        timeout: float = 10.0,
        max_pages: int = 100,
    ) -> None:
        self._http_get = http_get
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self.max_pages = max(1, int(max_pages))

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        http_get = self._http_get
        if http_get is None:
            import requests

            http_get = requests.get
        response = http_get(url, params=params, timeout=self.timeout)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        return response.json() if hasattr(response, "json") else response

    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[FundingRateRecord]:
        start_ms = _epoch_ms(start)
        end_ms = _epoch_ms(end)
        records: list[FundingRateRecord] = []
        cursor = start_ms
        for _ in range(self.max_pages):
            if cursor > end_ms:
                break
            payload = self._get(
                f"{self.base_url}{self.PATH}",
                {
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": self.PAGE_LIMIT,
                },
            )
            rows = payload if isinstance(payload, list) else []
            last_ms = None
            for row in rows:
                parsed = _parse_binance_row(symbol, row)
                if parsed is None:
                    continue
                record, funding_ms = parsed
                if start_ms <= funding_ms <= end_ms:
                    records.append(record)
                last_ms = funding_ms if last_ms is None else max(last_ms, funding_ms)
            if len(rows) < self.PAGE_LIMIT or last_ms is None:
                break
            cursor = last_ms + 1
        return records


class FundingRateRepository:
    """PostgreSQL storage for funding settlements."""

    def load(
        self,
        exchange_id: str,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[FundingRateRecord]:
        from app.utils.db import get_db_connection

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT funding_time, funding_rate, mark_price
                FROM qd_perpetual_funding_rates
                WHERE exchange_id = ? AND symbol = ? AND funding_time >= ? AND funding_time <= ?
                ORDER BY funding_time
                """,
                (
                    exchange_id,
                    symbol,
                    utc_naive(start).to_pydatetime(),
                    utc_naive(end).to_pydatetime(),
                ),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            FundingRateRecord(
                exchange_id=exchange_id,
                symbol=symbol,
                funding_time=utc_naive(row["funding_time"]),
                rate=float(row["funding_rate"]),
                mark_price=float(row["mark_price"]) if row.get("mark_price") is not None else None,
            )
            for row in rows
        ]

    def upsert(self, records: Iterable[FundingRateRecord], *, source: str) -> int:
        from app.utils.db import get_db_connection

        rows = list(records)
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            for record in rows:
                cur.execute(
                    """
                    INSERT INTO qd_perpetual_funding_rates
                      (exchange_id, symbol, funding_time, funding_rate, mark_price, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (exchange_id, symbol, funding_time) DO UPDATE SET
                      funding_rate = EXCLUDED.funding_rate,
                      mark_price = EXCLUDED.mark_price,
                      source = EXCLUDED.source,
                      fetched_at = NOW()
                    """,
                    (
                        record.exchange_id,
                        record.symbol,
                        utc_naive(record.funding_time).to_pydatetime(),
                        float(record.rate),
                        record.mark_price,
                        source,
                    ),
                )
            db.commit()
            cur.close()
        return len(rows)


class FundingRateHistoryService:
    """Read stored funding history and backfill the missing windows first."""

    def __init__(
        self,
        *,
        repository: FundingRateRepository | None = None,
        clients: dict[str, FundingRateClient] | None = None,
    ) -> None:
        self.repository = repository or FundingRateRepository()
        self.clients: dict[str, FundingRateClient] = (
            dict(clients)
            if clients is not None
            else {BINANCE_EXCHANGE_ID: BinanceUsdmFundingRateClient()}
        )

    def supports(self, exchange_id: str) -> bool:
        return str(exchange_id or "").strip().lower() in self.clients

    def history(
        self,
        *,
        exchange_id: str,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> FundingHistory:
        exchange = str(exchange_id or "").strip().lower()
        client = self.clients.get(exchange)
        if client is None:
            raise FundingRateUnsupportedError(f"strategyV2.fundingExchangeUnsupported:{exchange}")
        start_ts = utc_naive(start)
        end_ts = utc_naive(end)
        result = FundingHistory(exchange_id=exchange, symbol=symbol)

        stored = self.repository.load(exchange, symbol, start_ts, end_ts)
        windows = backfill_windows([item.funding_time for item in stored], start_ts, end_ts)
        fetched: list[FundingRateRecord] = []
        for window_start, window_end in windows:
            try:
                fetched.extend(client.fetch(symbol, window_start.to_pydatetime(), window_end.to_pydatetime()))
            except Exception as exc:
                message = str(exc)[:240] or exc.__class__.__name__
                logger.warning(
                    "Funding rate backfill failed exchange=%s symbol=%s window=%s..%s: %s",
                    exchange, symbol, window_start, window_end, message,
                )
                result.fetch_errors.append(message)
        if fetched:
            try:
                self.repository.upsert(fetched, source=BINANCE_USDM_SOURCE)
            except Exception as exc:
                message = str(exc)[:240] or exc.__class__.__name__
                logger.warning("Funding rate persistence failed exchange=%s symbol=%s: %s", exchange, symbol, message)
                result.fetch_errors.append(message)
        result.fetched = len(fetched)

        merged: dict[pd.Timestamp, FundingRateRecord] = {item.funding_time: item for item in stored}
        for item in fetched:
            if start_ts <= item.funding_time <= end_ts:
                merged[item.funding_time] = item
        result.records = [merged[key] for key in sorted(merged)]
        result.missing_periods = missing_funding_periods(
            [item.funding_time for item in result.records],
            start_ts,
            end_ts,
        )
        return result


def backfill_windows(
    times: Iterable[pd.Timestamp],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return the windows of ``[start, end]`` that stored data does not cover."""
    ordered = sorted({utc_naive(item) for item in times})
    if not ordered:
        return [(start, end)]
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for period in missing_funding_periods(ordered, start, end):
        windows.append((utc_naive(period["start"]), utc_naive(period["end"])))
    if len(ordered) == 1:
        # One settlement reveals no cadence; refresh both sides of it.
        if ordered[0] > start:
            windows.append((start, ordered[0]))
        if ordered[0] < end:
            windows.append((ordered[0], end))
    return _merge_windows(windows)


def missing_funding_periods(
    times: Iterable[pd.Timestamp],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    """Locate gaps in a settlement series using its own local cadence.

    ``start``/``end`` of each reported period are exclusive bounds: the last
    known settlement (or range start) and the next known settlement (or range
    end). ``missingCount`` is the number of settlements the local cadence
    expects inside the period; it is ``None`` when no data exists at all.
    """
    start_ts = utc_naive(start)
    end_ts = utc_naive(end)
    ordered = sorted({utc_naive(item) for item in times if start_ts <= utc_naive(item) <= end_ts})
    if not ordered:
        if end_ts <= start_ts:
            return []
        return [_period(start_ts, end_ts, None, "no_funding_data")]
    intervals = [
        (ordered[index + 1] - ordered[index]).total_seconds()
        for index in range(len(ordered) - 1)
    ]
    periods: list[dict[str, Any]] = []
    if intervals:
        head_count = _expected_count(ordered[0] - start_ts, intervals[0])
        if head_count > 0:
            periods.append(_period(start_ts, ordered[0], head_count, "range_start"))
    for index, interval in enumerate(intervals):
        neighbors = [
            intervals[item]
            for item in (index - 1, index + 1)
            if 0 <= item < len(intervals)
        ]
        if not neighbors:
            continue
        reference = max(neighbors)
        if reference > 0 and interval > reference * _GAP_TOLERANCE:
            count = max(1, int(round(interval / reference)) - 1)
            periods.append(_period(ordered[index], ordered[index + 1], count, "gap"))
    if intervals:
        tail_count = _expected_count(end_ts - ordered[-1], intervals[-1])
        if tail_count > 0:
            periods.append(_period(ordered[-1], end_ts, tail_count, "range_end"))
    return periods


def _expected_count(span: pd.Timedelta, interval_seconds: float) -> int:
    if interval_seconds <= 0:
        return 0
    # A small epsilon absorbs millisecond jitter in exchange timestamps.
    return max(0, int(math.floor(span.total_seconds() / interval_seconds + 1e-6)))


def _period(start: pd.Timestamp, end: pd.Timestamp, count: int | None, reason: str) -> dict[str, Any]:
    return {
        "start": _iso(start),
        "end": _iso(end),
        "missingCount": count,
        "reason": reason,
    }


def _merge_windows(
    windows: list[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    merged: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for window_start, window_end in sorted(windows):
        if window_end < window_start:
            continue
        if merged and window_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], window_end))
        else:
            merged.append((window_start, window_end))
    return merged


def _iso(value: pd.Timestamp) -> str:
    return utc_naive(value).tz_localize("UTC").isoformat().replace("+00:00", "Z")


def _epoch_ms(value: Any) -> int:
    timestamp = utc_naive(value).tz_localize("UTC")
    return int(timestamp.value // 1_000_000)


def _parse_binance_row(symbol: str, row: Any) -> tuple[FundingRateRecord, int] | None:
    if not isinstance(row, dict):
        return None
    try:
        funding_ms = int(row.get("fundingTime"))
        rate = float(row.get("fundingRate"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate):
        return None
    try:
        mark_price = float(row.get("markPrice")) if row.get("markPrice") not in (None, "") else None
    except (TypeError, ValueError):
        mark_price = None
    timestamp = pd.Timestamp(funding_ms, unit="ms")
    return (
        FundingRateRecord(
            exchange_id=BINANCE_EXCHANGE_ID,
            symbol=symbol,
            funding_time=timestamp,
            rate=rate,
            mark_price=mark_price if mark_price is not None and math.isfinite(mark_price) else None,
        ),
        funding_ms,
    )


_default_service: FundingRateHistoryService | None = None


def get_funding_rate_history_service() -> FundingRateHistoryService:
    global _default_service
    if _default_service is None:
        _default_service = FundingRateHistoryService()
    return _default_service
