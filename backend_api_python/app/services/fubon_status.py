"""Read-only connection status of the Fubon market data sidecar.

The account center shows Fubon as a market-data-only source: it is never a
trading venue, has no saved credentials in the database and exposes no order,
account or position capability. This module asks the sidecar ``GET /health``
(``FUBON_SIDECAR_URL`` + ``X-Sidecar-Token``) and maps the answer to a small,
whitelisted payload.

Every failure is reported as ``connected: false`` plus a machine-readable
``error`` code instead of an exception. Neither the payload nor the log lines
carry the sidecar token, the Fubon login id, passwords, API keys, certificate
paths or the sidecar URL. Results are cached in-process for a short TTL so a
refreshing account center cannot flood the sidecar.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

import requests

from app.config.data_sources import FubonSidecarConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TOKEN_HEADER = "X-Sidecar-Token"
HEALTH_PATH = "/health"
CACHE_TTL_SECONDS = 15.0

# The sidecar only ever serves market data; QuantDinger never routes orders to it.
CAPABILITIES = ("market_data",)

ERROR_UNREACHABLE = "sidecarUnreachable"
ERROR_TIMEOUT = "sidecarTimeout"
ERROR_UNAUTHORIZED = "sidecarUnauthorized"
ERROR_INVALID_RESPONSE = "sidecarInvalidResponse"
ERROR_HTTP = "sidecarError"

_MAX_SDK_VERSION_LENGTH = 32

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "expires_at": 0.0, "status": None}


def _checked_at() -> str:
    return datetime.now(tz=TAIPEI_TZ).isoformat(timespec="seconds")


def _base_status(*, configured: bool) -> Dict[str, Any]:
    return {
        "configured": configured,
        "connected": False,
        "logged_in": False,
        "realtime_ready": False,
        "sdk_version": None,
        "capabilities": list(CAPABILITIES),
        "trading": False,
        "checked_at": _checked_at(),
    }


def _failure(error: str) -> Dict[str, Any]:
    status = _base_status(configured=True)
    status["error"] = error
    return status


def _sdk_version(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:_MAX_SDK_VERSION_LENGTH] or None


def _probe(base_url: str, token: str, timeout: float) -> Dict[str, Any]:
    """Call the sidecar ``/health`` once and map the answer to a status payload."""
    try:
        resp = requests.get(
            f"{base_url}{HEALTH_PATH}",
            headers={TOKEN_HEADER: token},
            timeout=timeout,
        )
    except requests.Timeout as exc:
        logger.warning("Fubon sidecar health check timed out (%s)", type(exc).__name__)
        return _failure(ERROR_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Fubon sidecar health check unreachable (%s)", type(exc).__name__)
        return _failure(ERROR_UNREACHABLE)

    if resp.status_code == 401:
        logger.warning("Fubon sidecar rejected X-Sidecar-Token on the health check (401)")
        return _failure(ERROR_UNAUTHORIZED)
    if resp.status_code != 200:
        logger.warning("Fubon sidecar health check failed (HTTP %s)", resp.status_code)
        return _failure(ERROR_HTTP)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        logger.warning("Fubon sidecar health check returned a non-object response")
        return _failure(ERROR_INVALID_RESPONSE)

    status = _base_status(configured=True)
    status.update(
        {
            "connected": True,
            "logged_in": body.get("logged_in") is True,
            "realtime_ready": body.get("realtime_ready") is True,
            "sdk_version": _sdk_version(body.get("sdk_version")),
        }
    )
    return status


def _cache_key(base_url: str, token: str, timeout: float) -> str:
    # Hash the settings so a config change invalidates the cache without keeping
    # the raw token in another structure.
    raw = f"{base_url}\n{token}\n{timeout}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def get_fubon_status(*, clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Return the cached Fubon sidecar status, probing the sidecar when stale."""
    token = FubonSidecarConfig.TOKEN
    if not token:
        return _base_status(configured=False)

    base_url = FubonSidecarConfig.BASE_URL
    timeout = FubonSidecarConfig.TIMEOUT
    key = _cache_key(base_url, token, timeout)
    with _cache_lock:
        if _cache["key"] == key and clock() < _cache["expires_at"] and _cache["status"]:
            return dict(_cache["status"])
        # Probe while holding the lock so concurrent refreshes share one request.
        status = _probe(base_url, token, timeout)
        _cache.update(key=key, expires_at=clock() + CACHE_TTL_SECONDS, status=dict(status))
    return status


def reset_cache() -> None:
    """Drop the cached status (tests and config reloads)."""
    with _cache_lock:
        _cache.update(key=None, expires_at=0.0, status=None)
