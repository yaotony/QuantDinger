"""Health and self-introspection endpoints (class R, but token-free for /health)."""
from __future__ import annotations

from datetime import datetime, timezone

from flask import jsonify

from app._version import APP_VERSION
from app.services.agent_environment import build_environment_report
from app.utils.agent_auth import (
    SCOPE_R, agent_required, current_token, current_user_id,
)
from app.utils.agent_jobs import concurrent_job_limits, count_active_jobs
from app.utils.logger import get_logger

from . import agent_v1_bp
from ._helpers import envelope

logger = get_logger(__name__)


@agent_v1_bp.route("/health", methods=["GET"])
def health():
    """Public liveness probe. Does NOT require a token.

    Useful for health checks from monitoring tools and from agent SDKs that
    want to confirm the gateway is reachable before issuing real calls.
    """
    return jsonify({
        "service": "quantdinger-agent-gateway",
        "version": "v1",
        "status": "ok",
        # SafeJSONProvider serializes datetimes as UTC ISO (with Z).
        "timestamp": datetime.now(timezone.utc),
    }), 200


@agent_v1_bp.route("/whoami", methods=["GET"])
@agent_required(SCOPE_R)
def whoami():
    """Return the calling token's identity and granted capabilities.

    Lets agents self-discover scopes / market allowlists without guessing.
    Secrets (token hash, etc.) are never returned.
    """
    token = current_token()
    return envelope({
        "user_id": current_user_id(),
        "agent_name": token.get("name"),
        "scopes": (token.get("scopes") or "R").split(","),
        "markets": (token.get("markets") or "*").split(","),
        "instruments": (token.get("instruments") or "*").split(","),
        "paper_only": bool(token.get("paper_only", True)),
        "rate_limit_per_min": int(token.get("rate_limit_per_min") or 60),
        "max_order_notional": float(token.get("max_order_notional") or 1000),
        "max_daily_notional": float(token.get("max_daily_notional") or 5000),
    })


@agent_v1_bp.route("/environment", methods=["GET"])
@agent_required(SCOPE_R)
def environment():
    """Readiness report: version, scopes, paper-only mode, backtest quota and range limits.

    Each check is ``pass``/``warn``/``fail`` with a remediation hint. The token
    value, hash and id are never returned.
    """
    user_id = current_user_id()
    token = current_token()
    tenant_limit, token_limit = concurrent_job_limits()
    try:
        tenant_active = count_active_jobs(user_id=user_id)
        token_active = count_active_jobs(
            user_id=user_id, agent_token_id=int(token.get("id") or 0),
        )
    except Exception:
        logger.exception("agent_v1/environment: active job count unavailable")
        tenant_active = token_active = None
    return envelope(build_environment_report(
        token=token,
        app_version=APP_VERSION,
        tenant_limit=tenant_limit,
        token_limit=token_limit,
        tenant_active=tenant_active,
        token_active=token_active,
    ))
