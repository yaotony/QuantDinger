"""Periodic maintenance tasks managed by Celery Beat."""

from __future__ import annotations

import os
import socket

from app.celery_app import celery_app


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@celery_app.task(name="quantdinger.tasks.worker_heartbeat")
def record_worker_heartbeat() -> None:
    from app.services.strategy_command_repository import StrategyCommandRepository

    StrategyCommandRepository().record_worker_heartbeat(
        worker_id=f"celery:{socket.gethostname()}",
        role="celery",
        metadata={},
    )


@celery_app.task(name="quantdinger.tasks.cleanup_runtime_metadata")
def cleanup_runtime_metadata() -> dict:
    from app.services.strategy_command_repository import StrategyCommandRepository

    return StrategyCommandRepository().cleanup_runtime_metadata(
        command_retention_days=max(1, int(os.getenv("STRATEGY_COMMAND_RETENTION_DAYS", "30"))),
        heartbeat_retention_days=max(1, int(os.getenv("WORKER_HEARTBEAT_RETENTION_DAYS", "7"))),
    )


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.reflection",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_reflection(self):
    del self
    if not _enabled("ENABLE_REFLECTION_WORKER", default="false"):
        return {"skipped": True}
    from app.services.reflection import ReflectionService

    return ReflectionService().run_verification_cycle()


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.ai_calibration",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_ai_calibration(self):
    del self
    from app.services.ai_calibration import confidence_calibration_enabled
    if not confidence_calibration_enabled():
        return {"skipped": True}
    from app.services.ai_calibration import AICalibrationService

    service = AICalibrationService()
    results = []
    markets = os.getenv("AI_CALIBRATION_MARKETS", "Crypto").split(",")
    for market in markets:
        market = market.strip()
        if not market:
            continue
        result = service.calibrate_market(
            market=market,
            lookback_days=int(os.getenv("AI_CALIBRATION_LOOKBACK_DAYS", "30")),
            min_samples=int(os.getenv("AI_CALIBRATION_MIN_SAMPLES", "80")),
        )
        if result is not None:
            results.append(result.__dict__)
    return {"markets": results}


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.market_catalog_sync",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_market_catalog_sync(self):
    del self
    if not _enabled("MARKET_CATALOG_AUTO_SYNC"):
        return {"skipped": True}
    from app.services.market_catalog_sync import run_market_catalog_sync_inline

    return run_market_catalog_sync_inline("celery-beat")


@celery_app.task(
    name="quantdinger.tasks.tw_kline_collect",
    soft_time_limit=max(60, int(os.getenv("TW_KLINE_COLLECT_TIME_LIMIT_SEC", "540"))),
)
def run_tw_kline_collect():
    """Store Taiwan 1m K-lines after each session; pending days are retried by the next tick."""
    from app.services.tw_kline_collector import run_tw_kline_collection

    return run_tw_kline_collection()


@celery_app.task(
    name="quantdinger.tasks.taifex_tick_backfill",
    soft_time_limit=max(60, int(os.getenv("TAIFEX_TICK_BACKFILL_TIME_LIMIT_SEC", "900"))),
)
def run_taifex_tick_backfill():
    """Fill Fubon 1m gaps from the previous trading days' TAIFEX tick files."""
    from app.services.taifex_tick_backfill import run_taifex_tick_backfill as run_backfill

    return run_backfill()
