"""Async V2 backtest endpoints (class B).

Submit returns a job_id; the agent polls /jobs/{id} until done. This endpoint
uses the same V2 execution model as the human Backtest Center.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.services.backtest_jobs import parse_backtest_date, run_backtest_job
from app.services.backtest_limits import BacktestRangeLimitError
from app.services.billing_service import BillingError
from app.services.script_source import get_script_source_service
from app.services.strategy_v2 import StrategyV2BacktestService
from app.services.strategy_v2.service import precheck_backtest_range
from app.utils.agent_auth import (
    SCOPE_B, agent_required, current_token, current_user_id,
    instrument_allowed, market_allowed, with_idempotency,
)
from app.utils.agent_jobs import _job_receipt, count_active_jobs, submit_job
from app.utils.logger import get_logger
from flask import request

from . import agent_v1_bp
from ._helpers import envelope, error, get_json_or_400
from ._security import assert_indicator_code_size

logger = get_logger(__name__)
_backtest = StrategyV2BacktestService()
_BACKTEST_FIELDS = {
    "code", "sourceId", "sourceVersion",
    "startDate", "endDate", "initialCapital", "commission", "slippage",
    "leverageEnabled", "leverage", "params",
    "instrumentRulesSnapshotId",
}
# Server-resolved strategy source reference stored on the job request. The
# runner loads code from this pinned, tenant-owned version snapshot.
SOURCE_REF_KEY = "__strategy_source"


def _token_has_restricted_allowlist(field: str) -> bool:
    raw = str(current_token().get(field) or "*").strip()
    return raw not in {"", "*"}


def _optional_positive_int(body: dict, field: str) -> tuple[int | None, Any]:
    value = body.get(field)
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, error(400, f"{field} must be a positive integer")
    return value, None


def _is_hidden_source(source: dict) -> bool:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    return bool(source.get("code_hidden") or metadata.get("code_hidden"))


def _resolve_source(body: dict) -> tuple[str, dict | None, Any]:
    """Resolve inline ``code`` or a tenant-owned ``sourceId`` (+ version).

    Returns ``(code, source_ref, error_response)``. ``source_ref`` pins the
    exact version snapshot so the job runs the code that was validated here.
    """
    source_id, err = _optional_positive_int(body, "sourceId")
    if err:
        return "", None, err
    version_no, err = _optional_positive_int(body, "sourceVersion")
    if err:
        return "", None, err
    code = str(body.get("code") or "").strip()
    if code and source_id:
        return "", None, error(400, "Provide either code or sourceId, not both")
    if version_no and not source_id:
        return "", None, error(400, "sourceVersion requires sourceId")
    if not source_id:
        if not code:
            return "", None, error(400, "code or sourceId is required")
        return code, None, None

    sources = get_script_source_service()
    user_id = current_user_id()
    # Ownership is checked against the caller's tenant; another tenant's
    # source is indistinguishable from a missing one.
    source = sources.get_source(source_id, user_id=user_id)
    if not source:
        return "", None, error(404, "Strategy source not found", http=404)
    if _is_hidden_source(source):
        return "", None, error(403, "Strategy source code is hidden", http=403)
    if version_no:
        version = sources.get_version_by_number(source_id, version_no, user_id)
        if not version:
            return "", None, error(404, "Strategy source version not found", http=404)
    else:
        version = sources.get_latest_version(source_id, user_id=user_id)
        if not version:
            return "", None, error(409, "Strategy source has no saved version", http=409)
    code = str(version.get("code") or "").strip()
    if not code:
        return "", None, error(400, "Strategy source version has no code")
    ref = {
        "sourceId": int(source_id),
        "versionId": int(version.get("id") or 0),
        "versionNo": int(version.get("version_no") or 0),
        "name": str(source.get("name") or ""),
    }
    return code, ref, None


@dataclass(frozen=True)
class ValidatedBacktest:
    program: Any
    source_ref: dict | None = None


def _validate_request(body: dict) -> tuple[ValidatedBacktest | None, Any]:
    unsupported = sorted(set(body) - _BACKTEST_FIELDS)
    if unsupported:
        return None, error(400, f"Unsupported backtest fields: {', '.join(unsupported)}")
    code, source_ref, err = _resolve_source(body)
    if err:
        return None, err
    program, err = _validate_code(body, code)
    if err:
        return None, err
    return ValidatedBacktest(program=program, source_ref=source_ref), None


def _validate_code(body: dict, code: str) -> tuple[Any, Any]:
    try:
        assert_indicator_code_size(code)
    except ValueError as exc:
        return None, error(400, str(exc))

    try:
        start_date = _parse_date(body.get("startDate"))
        end_date = _parse_date(body.get("endDate"))
    except ValueError as exc:
        return None, error(400, str(exc))
    if not start_date or not end_date:
        return None, error(400, "startDate and endDate are required (YYYY-MM-DD)")
    if end_date < start_date:
        return None, error(400, "endDate must not be earlier than startDate")

    params = body.get("params", {})
    if not isinstance(params, dict):
        return None, error(400, "params must be an object")
    try:
        if float(body.get("initialCapital") or 10000) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return None, error(400, "initialCapital must be a positive number")

    try:
        from app.services.strategy_v2 import compile_strategy_v2

        program = compile_strategy_v2(code)
        manifest = program.manifest
        metadata = manifest.metadata()
    except Exception as exc:
        return None, error(400, str(exc), http=400)

    for market in manifest.markets:
        if not market_allowed(market):
            return None, error(403, f"Market not allowed: {market}", http=403)

    instruments: list[dict] = list((metadata.get("universe") or {}).get("instruments") or [])
    for subscription in metadata.get("subscriptions") or []:
        instruments.extend(subscription.get("instruments") or [])
    if isinstance(metadata.get("benchmark"), dict):
        instruments.append(metadata["benchmark"])

    seen: set[tuple[str, str]] = set()
    for instrument in instruments:
        market = str(instrument.get("market") or "")
        symbol = str(instrument.get("symbol") or "")
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        if market and not market_allowed(market):
            return None, error(403, f"Market not allowed: {market}", http=403)
        if symbol and not instrument_allowed(symbol):
            return None, error(403, f"Instrument not allowed: {symbol}", http=403)

    universe = metadata.get("universe") or {}
    dynamic_references = [str(universe.get("reference") or "").strip()]
    dynamic_references.extend(
        str(item.get("universeReference") or "").strip()
        for item in metadata.get("subscriptions") or []
    )
    if any(dynamic_references) and _token_has_restricted_allowlist("instruments"):
        return None, error(
            403,
            "Dynamic universes require an unrestricted instrument allowlist",
            http=403,
        )

    # Reject oversized ranges (including indicator warmup) before a job is
    # accepted and billed; details carry limits and suggested dates.
    try:
        precheck_backtest_range(manifest, start_date=start_date, end_date=end_date)
    except BacktestRangeLimitError as exc:
        return None, error(400, str(exc), details=exc.details, http=400)

    return program, None


def _parse_date(s: Any) -> Any:
    return parse_backtest_date(s)


def _public_source_ref(ref: dict) -> dict:
    return {key: ref.get(key) for key in ("sourceId", "versionId", "versionNo")}


def _load_pinned_source(payload: dict, ref: dict) -> dict:
    """Return a run payload carrying the code of the pinned source version."""
    user_id = int(payload.get("__user_id") or 0)
    source_id = int(ref.get("sourceId") or 0)
    version_id = int(ref.get("versionId") or 0)
    version = None
    if user_id and source_id and version_id:
        version = get_script_source_service().get_version(version_id, user_id)
    if not version or int(version.get("source_id") or 0) != source_id:
        raise ValueError("strategyV2.sourceVersionNotFound")
    run_payload = dict(payload)
    run_payload["code"] = str(version.get("code") or "")
    run_payload["__source_id"] = source_id
    run_payload["__strategy_name"] = str(ref.get("name") or "")
    return run_payload


def _run_backtest(payload: dict, on_progress=None) -> Any:
    ref = payload.get(SOURCE_REF_KEY)
    if not isinstance(ref, dict):
        return run_backtest_job(payload, on_progress, service=_backtest)
    result = run_backtest_job(_load_pinned_source(payload, ref), on_progress, service=_backtest)
    return {**result, "strategySource": _public_source_ref(ref)}


@agent_v1_bp.route("/backtest/run", methods=["POST"])
@agent_required(SCOPE_B)
def create_backtest():
    """Submit a backtest job. Returns 202 with `job_id` for polling."""
    body, err = get_json_or_400()
    if err:
        return err

    validated, validation_error = _validate_request(body)
    if validation_error:
        return validation_error
    source_ref = validated.source_ref if validated else None

    with with_idempotency("backtest") as existing:
        if existing:
            return envelope(_job_receipt(existing, duplicate=True), message="idempotent replay")

    token_id = int(current_token().get("id") or 0)
    tenant_cap = max(1, int(os.getenv("AGENT_MAX_CONCURRENT_JOBS_PER_TENANT", "4")))
    token_cap = max(1, int(os.getenv("AGENT_MAX_CONCURRENT_JOBS_PER_TOKEN", "2")))
    if count_active_jobs(user_id=current_user_id()) >= tenant_cap:
        return error(
            429,
            "Tenant concurrent job limit reached",
            details={"limit": tenant_cap},
            retriable=True,
            http=429,
        )
    if count_active_jobs(user_id=current_user_id(), agent_token_id=token_id) >= token_cap:
        return error(
            429,
            "Token concurrent job limit reached",
            details={"limit": token_cap},
            retriable=True,
            http=429,
        )

    payload = dict(body)
    payload["__user_id"] = current_user_id()
    if source_ref:
        payload[SOURCE_REF_KEY] = source_ref
    try:
        job = submit_job(
            user_id=current_user_id(),
            agent_token_id=token_id,
            kind="backtest",
            request_payload=payload,
            runner=_run_backtest,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except BillingError as exc:
        return error(exc.status, exc.code, details={"error_type": exc.code, **exc.details},
                     retriable=exc.status >= 500, http=exc.status)
    except Exception:
        logger.exception("Agent backtest submission failed")
        return error(503, "BILLING_OR_JOB_UNAVAILABLE", retriable=True, http=503)
    if job["status"] == "failed":
        return error(503, "AGENT_JOB_DISPATCH_FAILED", details=job, http=503)
    if source_ref:
        job = {**job, "strategySource": _public_source_ref(source_ref)}
    return envelope(job, message="queued", status=202)
