"""Agent environment readiness report for GET /api/agent/v1/environment.

The report lets an agent confirm, in one call, what its token may do and
whether backtests can be submitted right now. Each check carries a status
(``pass``, ``warn`` or ``fail``) and, unless it passes, a remediation hint.

Nothing secret is ever included: no token value, hash, or token id.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.services.backtest_limits import backtest_range_limit_summary

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
_SEVERITY = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}

TOKEN_EXPIRY_WARN_DAYS = 7
_REISSUE_HINT = (
    "Issue a new token via POST /api/agent/v1/me/tokens (human session) with the "
    "needed scopes, set it as QUANTDINGER_AGENT_TOKEN, and revoke the old one."
)


def _split(raw: Any, default: str) -> List[str]:
    return [part.strip() for part in str(raw or default).split(",") if part.strip()]


def _check(check_id: str, status: str, message: str, remediation: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "remediation": None if status == STATUS_PASS else remediation,
    }


def worst_status(statuses: Iterable[str]) -> str:
    """Return the most severe status; unknown values count as ``fail``."""
    worst = STATUS_PASS
    for status in statuses:
        status = status if status in _SEVERITY else STATUS_FAIL
        if _SEVERITY[status] > _SEVERITY[worst]:
            worst = status
    return worst


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _scope_check(scopes: List[str]) -> Dict[str, Any]:
    granted = ",".join(scopes)
    if "B" not in scopes:
        return _check(
            "scopes", STATUS_FAIL,
            f"Token scopes are {granted}; the B scope is missing, so backtests cannot be submitted.",
            "Token lacks the B scope. " + _REISSUE_HINT,
        )
    if "W" not in scopes:
        return _check(
            "scopes", STATUS_WARN,
            f"Token scopes are {granted}; the W scope is missing, so strategy sources "
            "and strategies cannot be saved.",
            "Add the W scope if the agent must save sources. " + _REISSUE_HINT,
        )
    return _check("scopes", STATUS_PASS, f"Token scopes are {granted}.")


def _trading_mode_check(scopes: List[str], paper_only: bool) -> Dict[str, Any]:
    if paper_only:
        return _check("paper_only", STATUS_PASS, "Token is limited to paper trading.")
    if "T" in scopes:
        return _check(
            "paper_only", STATUS_WARN,
            "Token is live-capable (T scope with paper_only=false); orders may reach real "
            "exchange accounts when server live-trading flags allow it.",
            "Unless live trading is intended, revoke this token and issue one with "
            "paper_only=true and without the T scope via POST /api/agent/v1/me/tokens.",
        )
    return _check(
        "paper_only", STATUS_PASS,
        "Token is not paper-only, but it has no T scope and cannot place orders.",
    )


def _quota_check(scopes: List[str], quota: Dict[str, Any]) -> Dict[str, Any]:
    if "B" not in scopes:
        return _check(
            "backtest_quota", STATUS_WARN,
            "Backtest quota does not apply: the token lacks the B scope.",
            "Token lacks the B scope. " + _REISSUE_HINT,
        )
    if quota["token_active"] is None or quota["tenant_active"] is None:
        return _check(
            "backtest_quota", STATUS_WARN,
            "Active backtest job count is unavailable.",
            "Retry later; if this persists, ask the operator to check the backend database.",
        )
    usage = (
        f"{quota['token_active']}/{quota['token_limit']} slots in use for this token, "
        f"{quota['tenant_active']}/{quota['tenant_limit']} for the tenant."
    )
    if quota["available"] <= 0:
        return _check(
            "backtest_quota", STATUS_WARN,
            f"No free backtest slot: {usage} submit_backtest will return HTTP 429.",
            "Wait for running jobs (list_jobs, wait_for_job) or cancel one with cancel_job.",
        )
    return _check("backtest_quota", STATUS_PASS, usage)


def _expiry_check(expires_at: Any, now: datetime) -> Dict[str, Any]:
    if not isinstance(expires_at, datetime):
        if expires_at:
            return _check("token_expiry", STATUS_PASS, f"Token expires at {_iso(expires_at)}.")
        return _check("token_expiry", STATUS_PASS, "Token does not expire.")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at - now <= timedelta(days=TOKEN_EXPIRY_WARN_DAYS):
        return _check(
            "token_expiry", STATUS_WARN,
            f"Token expires soon, at {_iso(expires_at)}.",
            "Issue a replacement token via POST /api/agent/v1/me/tokens before it expires.",
        )
    return _check("token_expiry", STATUS_PASS, f"Token expires at {_iso(expires_at)}.")


def build_environment_report(
    *,
    token: Dict[str, Any],
    app_version: str,
    tenant_limit: int,
    token_limit: int,
    tenant_active: Optional[int],
    token_active: Optional[int],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the readiness report for the calling token.

    ``tenant_active``/``token_active`` are ``None`` when the job count could
    not be read; the quota check then warns instead of failing the call.
    """
    now = now or datetime.now(timezone.utc)
    scopes = _split(token.get("scopes"), "R")
    markets = _split(token.get("markets"), "*")
    paper_only = bool(token.get("paper_only", True))

    available = None
    if tenant_active is not None and token_active is not None:
        available = max(0, min(tenant_limit - tenant_active, token_limit - token_active))
    quota = {
        "tenant_limit": int(tenant_limit),
        "tenant_active": tenant_active,
        "token_limit": int(token_limit),
        "token_active": token_active,
        "available": available,
    }

    checks = [
        _scope_check(scopes),
        _trading_mode_check(scopes, paper_only),
        _quota_check(scopes, quota),
        _check(
            "backtest_range_limits", STATUS_PASS,
            "Backtest range limits by market and timeframe are listed in "
            "backtest_range_limits (warmup included); use get_data_coverage "
            "(GET /api/agent/v1/data-coverage) for one symbol and date range.",
        ),
        _expiry_check(token.get("expires_at"), now),
    ]
    return {
        "service": "quantdinger-agent-gateway",
        "api_version": "v1",
        "app_version": str(app_version or "unknown"),
        "overall": worst_status(item["status"] for item in checks),
        "token": {
            "agent_name": token.get("name"),
            "scopes": scopes,
            "markets": markets,
            "instruments": _split(token.get("instruments"), "*"),
            "paper_only": paper_only,
            "rate_limit_per_min": int(token.get("rate_limit_per_min") or 60),
            "expires_at": _iso(token.get("expires_at")),
        },
        "backtest_quota": quota,
        "backtest_range_limits": backtest_range_limit_summary(markets),
        "checks": checks,
    }
