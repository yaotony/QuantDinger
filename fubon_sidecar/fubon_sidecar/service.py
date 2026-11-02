"""Fubon SDK wrapper: login, realtime token, rate-limited read-only market data calls.

The SDK object never leaves this module. The HTTP layer only sees normalized dicts and
:mod:`fubon_sidecar.errors` exceptions. Order entry, account and position APIs of the
SDK are intentionally never called.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime
from typing import Any, Callable, NoReturn

from . import normalize
from .config import Settings
from .errors import (
    InvalidRequestError,
    NotFoundError,
    NotReadyError,
    RateLimitedError,
    SidecarError,
    UpstreamError,
)

logger = logging.getLogger("fubon_sidecar")

SdkFactory = Callable[[], Any]
VersionProvider = Callable[[], "str | None"]

INTRADAY_TIMEFRAMES = ("1", "5", "10", "15", "30", "60")
HISTORICAL_PERIOD_TIMEFRAMES = ("D", "W", "M")
HISTORICAL_TIMEFRAMES = INTRADAY_TIMEFRAMES + HISTORICAL_PERIOD_TIMEFRAMES
SESSIONS = ("regular", "afterhours")
STOCK_TICKER_TYPES = ("EQUITY", "INDEX", "WARRANT", "ODDLOT")
STOCK_EXCHANGES = ("TWSE", "TPEx")
# Board filter of ``intraday.tickers``: TSE listed, OTC over-the-counter, ESB
# emerging (興櫃一般板), TIB Taiwan Innovation Board, PSB emerging strategic board.
STOCK_MARKETS = ("TSE", "OTC", "ESB", "TIB", "PSB")
FUTOPT_TICKER_TYPES = ("FUTURE", "OPTION")
MAX_HISTORICAL_RANGE_DAYS = 366
LOGIN_RETRY_DELAYS_SEC = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)


def default_sdk_factory() -> Any:
    from fubon_neo.sdk import FubonSDK  # imported lazily: the wheel exists only in the image

    return FubonSDK()


def default_sdk_version() -> str | None:
    try:
        import fubon_neo

        return str(fubon_neo.__version__)
    except Exception:  # noqa: BLE001 - the SDK is optional outside the image
        pass
    try:
        from importlib.metadata import version

        return version("fubon_neo")
    except Exception:  # noqa: BLE001
        return None


class Redactor:
    """Replaces configured secret values in free text before it is logged or returned."""

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def __call__(self, text: object) -> str:
        output = str(text)
        for secret in self._secrets:
            output = output.replace(secret, "***")
        return output


class RateLimiter:
    """Spaces upstream calls at least ``1 / rate`` seconds apart (thread safe)."""

    def __init__(
        self,
        rate_per_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / rate_per_sec
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self, max_wait: float) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            wait = slot - now
            if wait > max_wait:
                raise RateLimitedError("Sidecar request rate limit reached; retry shortly")
            self._next_slot = slot + self._interval
        if wait > 0:
            self._sleep(wait)


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _payload_status(payload: Any) -> int | None:
    if isinstance(payload, dict) and "statusCode" in payload and "data" not in payload:
        try:
            return int(payload["statusCode"])
        except (TypeError, ValueError):
            return None
    return None


def _parse_day(value: str | None, name: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        if text.lstrip("-").isdigit():
            return datetime.fromtimestamp(int(text), tz=normalize.TAIPEI).date()
        return date.fromisoformat(text[:10])
    except (ValueError, OverflowError, OSError) as exc:
        raise InvalidRequestError(f"{name} must be YYYY-MM-DD or unix seconds") from exc


def _require_symbol(symbol: str | None) -> str:
    text = (symbol or "").strip()
    if not text or len(text) > 32 or not all(ch.isalnum() or ch in "-_." for ch in text):
        raise InvalidRequestError("symbol is required and must be alphanumeric")
    return text


def _require_choice(value: str | None, name: str, choices: tuple[str, ...]) -> str:
    text = (value or "").strip()
    if text not in choices:
        raise InvalidRequestError(f"{name} must be one of {', '.join(choices)}")
    return text


class FubonMarketData:
    """Owns the Fubon SDK session and exposes read-only, normalized market data calls."""

    def __init__(
        self,
        settings: Settings,
        sdk_factory: SdkFactory = default_sdk_factory,
        *,
        version_provider: VersionProvider = default_sdk_version,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        cert_exists: Callable[[str], bool] = os.path.isfile,
        max_workers: int = 4,
    ) -> None:
        self._settings = settings
        self._sdk_factory = sdk_factory
        self._version_provider = version_provider
        self._clock = clock
        self._sleep = sleep
        self._cert_exists = cert_exists
        self._redact = Redactor(settings.secret_values())
        self._limiter = RateLimiter(settings.rate_limit_per_sec, clock=clock, sleep=sleep)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fubon-rest")
        self._state_lock = threading.Lock()
        self._realtime_lock = threading.Lock()
        self._stop = threading.Event()
        self._sdk: Any = None
        self._logged_in = False
        self._realtime_ready = False
        self._fatal = False
        self._sdk_version: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            logged_in, realtime_ready, fatal = self._logged_in, self._realtime_ready, self._fatal
        if logged_in and realtime_ready:
            status = "ok"
        elif fatal:
            status = "error"
        else:
            status = "starting"
        if self._sdk_version is None:
            self._sdk_version = self._version_provider()
        return {
            "status": status,
            "logged_in": logged_in,
            "realtime_ready": realtime_ready,
            "sdk_version": self._sdk_version,
        }

    def login(self) -> bool:
        """Run one login + ``init_realtime`` attempt.

        Returns True when ready. Credential rejections and configuration problems are
        fatal (no retry, so a wrong password cannot lock the Fubon account); unexpected
        exceptions propagate so :meth:`run_login_loop` can retry them.
        """
        missing = self._settings.missing_credentials()
        if missing:
            logger.error("Fubon login skipped: missing environment variables %s", ", ".join(missing))
            self._mark_fatal()
            return False
        if not self._cert_exists(self._settings.cert_path):
            logger.error("Fubon login skipped: certificate file not found at FUBON_CERT_PATH")
            self._mark_fatal()
            return False

        sdk = self._sdk_factory()
        s = self._settings
        if s.uses_api_key:
            result = sdk.apikey_login(s.fubon_id, s.api_key, s.cert_path, s.cert_password)
        else:
            result = sdk.login(s.fubon_id, s.password, s.cert_path, s.cert_password)
        if not getattr(result, "is_success", False):
            reason = self._redact(getattr(result, "message", "") or "no message")
            logger.error("Fubon login rejected: %s", reason)
            self._mark_fatal()
            return False
        with self._state_lock:
            self._sdk = sdk
            self._logged_in = True
        logger.info("Fubon login succeeded (%s)", "api key" if s.uses_api_key else "password")

        sdk.init_realtime()
        with self._state_lock:
            self._realtime_ready = True
        logger.info("Fubon realtime market data initialized")
        return True

    def run_login_loop(self) -> None:
        """Retry :meth:`login` on transient errors with capped backoff until stopped."""
        attempt = 0
        while not self._stop.is_set():
            try:
                self.login()
                return
            except Exception as exc:  # noqa: BLE001 - every SDK failure is retried
                delay = LOGIN_RETRY_DELAYS_SEC[min(attempt, len(LOGIN_RETRY_DELAYS_SEC) - 1)]
                attempt += 1
                logger.warning(
                    "Fubon login attempt %d failed (%s: %s); retrying in %.0fs",
                    attempt,
                    type(exc).__name__,
                    self._redact(exc),
                    delay,
                )
                if self._stop.wait(delay):
                    return

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_login_loop, name="fubon-login", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _mark_fatal(self) -> None:
        with self._state_lock:
            self._fatal = True

    # ------------------------------------------------------------------ upstream calls

    def _ensure_ready(self) -> Any:
        with self._state_lock:
            if self._sdk is None or not self._logged_in or not self._realtime_ready:
                raise NotReadyError("Fubon session is not logged in or realtime is not ready")
            return self._sdk

    def _refresh_realtime(self, stale_sdk: Any) -> None:
        with self._realtime_lock:
            with self._state_lock:
                if self._sdk is not stale_sdk:
                    return
                self._realtime_ready = False
            try:
                stale_sdk.init_realtime()
            except Exception as exc:  # noqa: BLE001
                logger.error("Fubon realtime re-initialization failed (%s)", type(exc).__name__)
                raise UpstreamError("Fubon realtime token refresh failed") from exc
            with self._state_lock:
                self._realtime_ready = True
            logger.info("Fubon realtime token refreshed after upstream 401")

    def _request(self, market: str, path: tuple[str, ...], params: dict[str, Any]) -> Any:
        """Call ``sdk.marketdata.rest_client.<market>.<path>(**params)`` with limits."""
        deadline = self._clock() + self._settings.request_timeout_sec
        for attempt in range(2):
            sdk = self._ensure_ready()
            self._limiter.acquire(max(0.0, deadline - self._clock()))

            def call(sdk: Any = sdk) -> Any:
                target = getattr(sdk.marketdata.rest_client, market)
                for name in path:
                    target = getattr(target, name)
                return target(**params)

            future = self._executor.submit(call)
            try:
                payload = future.result(timeout=max(0.0, deadline - self._clock()))
            except FutureTimeout as exc:
                future.cancel()
                raise UpstreamError(
                    f"Fubon request timed out after {self._settings.request_timeout_sec:g}s"
                ) from exc
            except SidecarError:
                raise
            except Exception as exc:  # noqa: BLE001 - mapped onto the HTTP error contract
                status = _status_code(exc)
                if status == 401 and attempt == 0:
                    self._refresh_realtime(sdk)
                    continue
                self._raise_mapped(exc, status, market, path)
            status = _payload_status(payload)
            if status is not None and status >= 400:
                if status == 401 and attempt == 0:
                    self._refresh_realtime(sdk)
                    continue
                self._raise_mapped(None, status, market, path)
            return payload
        raise UpstreamError("Fubon rejected the realtime token")

    def _raise_mapped(
        self, exc: BaseException | None, status: int | None, market: str, path: tuple[str, ...]
    ) -> NoReturn:
        endpoint = ".".join((market, *path))
        if status == 404:
            raise NotFoundError("Fubon returned no data for this request") from exc
        kind = type(exc).__name__ if exc is not None else "HTTP error"
        logger.warning(
            "Fubon %s failed (%s%s): %s",
            endpoint,
            kind,
            f", HTTP {status}" if status else "",
            self._redact(exc) if exc is not None else "",
        )
        detail = f"HTTP {status}" if status else kind
        raise UpstreamError(f"Fubon market data request failed ({detail})") from exc

    # ------------------------------------------------------------------ public API

    def _candles_response(
        self, symbol: str, timeframe: str, payload: Any, session: str | None = None
    ) -> dict[str, Any]:
        candles = normalize.normalize_candles(normalize.data_rows(payload))
        if not candles:
            raise NotFoundError("Fubon returned no candles")
        body: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe}
        if session is not None:
            body["session"] = session
        body["candles"] = candles
        return body

    def stock_candles(
        self, symbol: str, timeframe: str, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        symbol = _require_symbol(symbol)
        timeframe = _require_choice(timeframe, "timeframe", HISTORICAL_TIMEFRAMES)
        start_day, end_day = _parse_day(start, "from"), _parse_day(end, "to")
        params: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe}
        if timeframe in HISTORICAL_PERIOD_TIMEFRAMES:
            if start_day and end_day:
                if start_day > end_day:
                    raise InvalidRequestError("from must not be after to")
                if (end_day - start_day).days > MAX_HISTORICAL_RANGE_DAYS:
                    raise InvalidRequestError("from/to range must be within one year")
            if start_day:
                params["from"] = start_day.isoformat()
            if end_day:
                params["to"] = end_day.isoformat()
        elif start_day or end_day:
            raise InvalidRequestError(
                "minute timeframes only return the latest 5 trading days; omit from/to"
            )
        payload = self._request("stock", ("historical", "candles"), params)
        return self._candles_response(symbol, timeframe, payload)

    def stock_intraday_candles(self, symbol: str, timeframe: str) -> dict[str, Any]:
        symbol = _require_symbol(symbol)
        timeframe = _require_choice(timeframe, "timeframe", INTRADAY_TIMEFRAMES)
        payload = self._request(
            "stock", ("intraday", "candles"), {"symbol": symbol, "timeframe": timeframe}
        )
        return self._candles_response(symbol, timeframe, payload)

    def stock_quote(self, symbol: str) -> dict[str, Any]:
        symbol = _require_symbol(symbol)
        payload = self._request("stock", ("intraday", "quote"), {"symbol": symbol})
        if not normalize.has_quote(payload):
            raise NotFoundError("Fubon returned no quote")
        return normalize.normalize_quote(payload, symbol)

    def stock_tickers(
        self, ticker_type: str, exchange: str | None = None, market: str | None = None
    ) -> dict[str, Any]:
        ticker_type = _require_choice(ticker_type, "type", STOCK_TICKER_TYPES)
        params: dict[str, Any] = {"type": ticker_type}
        if exchange:
            params["exchange"] = _require_choice(exchange, "exchange", STOCK_EXCHANGES)
        if market:
            params["market"] = _require_choice(market, "market", STOCK_MARKETS)
        payload = self._request("stock", ("intraday", "tickers"), params)
        tickers = normalize.normalize_tickers(
            normalize.data_rows(payload), normalize.STOCK_TICKER_FIELDS
        )
        if not tickers:
            raise NotFoundError("Fubon returned no tickers")
        return {
            "type": ticker_type,
            "exchange": params.get("exchange"),
            "market": params.get("market"),
            "tickers": tickers,
        }

    @staticmethod
    def _session_params(session: str | None) -> tuple[str, dict[str, Any]]:
        chosen = _require_choice(session or "regular", "session", SESSIONS)
        return chosen, ({"session": "afterhours"} if chosen == "afterhours" else {})

    def futopt_intraday_candles(
        self, symbol: str, timeframe: str, session: str | None = None
    ) -> dict[str, Any]:
        symbol = _require_symbol(symbol)
        timeframe = _require_choice(timeframe, "timeframe", INTRADAY_TIMEFRAMES)
        chosen, extra = self._session_params(session)
        payload = self._request(
            "futopt", ("intraday", "candles"), {"symbol": symbol, "timeframe": timeframe, **extra}
        )
        return self._candles_response(symbol, timeframe, payload, session=chosen)

    def futopt_quote(self, symbol: str, session: str | None = None) -> dict[str, Any]:
        symbol = _require_symbol(symbol)
        chosen, extra = self._session_params(session)
        payload = self._request("futopt", ("intraday", "quote"), {"symbol": symbol, **extra})
        if not normalize.has_quote(payload):
            raise NotFoundError("Fubon returned no quote")
        quote = normalize.normalize_quote(payload, symbol)
        quote["session"] = chosen
        return quote

    def futopt_tickers(self, ticker_type: str) -> dict[str, Any]:
        ticker_type = _require_choice(ticker_type, "type", FUTOPT_TICKER_TYPES)
        payload = self._request("futopt", ("intraday", "tickers"), {"type": ticker_type})
        tickers = normalize.normalize_tickers(
            normalize.data_rows(payload), normalize.FUTOPT_TICKER_FIELDS
        )
        if not tickers:
            raise NotFoundError("Fubon returned no tickers")
        return {"type": ticker_type, "tickers": tickers}
