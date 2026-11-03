"""Fix data stored for an unscheduled Taiwan market closure (typhoon day etc.).

One-off maintenance command, run by an operator after the closed day is listed
in ``BUILTIN_TW_MARKET_CLOSED_DAYS`` or ``TW_MARKET_CLOSED_DAYS`` (see
``app.services.tw_market_closure_repair``). It

1. deletes the ``TWFutures`` ``1D`` rows labelled on a closed day (the night
   session of the previous trading day, mis-attributed while the calendar still
   counted the closed day as a trading day),
2. moves ``failed`` rows of the closed day in ``qd_tw_kline_collect_status`` to
   ``missed`` with a note, and
3. re-imports the TAIFEX website daily report from the previous to the next
   trading day (idempotent upsert), which attributes the night session to the
   right trading day.

Dry run (default) prints what would change and touches nothing:
    python scripts/repair_tw_market_closed_days.py --days 2026-10-23

Apply:
    python scripts/repair_tw_market_closed_days.py --days 2026-10-23 --apply

Without ``--days`` every built-in and configured closed day is checked.
Run inside the backend environment (same database settings as the API).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, List, Optional, Sequence

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("SKIP_STARTUP_HOOKS", "1")

from app.data_sources.tw_futures_contracts import tw_market_closed_days  # noqa: E402
from app.services.taifex_history import parse_products  # noqa: E402
from app.services.tw_market_closure_repair import ClosedDayError, apply_plan, build_plan  # noqa: E402


def _days(text: Optional[str]) -> List[date]:
    if not text:
        return sorted(tw_market_closed_days())
    return sorted({date.fromisoformat(item.strip()) for item in text.split(",") if item.strip()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-attribute TWFutures 1D bars stored on unscheduled Taiwan market closures and mark "
            "their failed collection statuses as missed. Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--days",
        help="comma-separated closed days (YYYY-MM-DD); default: every built-in and configured closed day",
    )
    parser.add_argument(
        "--products",
        help="TAIFEX (TX,MTX,TMF) or Fubon (TXF,MXF) codes to re-import; default: the products of the deleted rows",
    )
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between TAIFEX requests")
    return parser


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    repository: Any = None,
    importer: Any = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        days = _days(args.days)
        products = parse_products(args.products) if args.products else None
    except ValueError as exc:
        parser.error(str(exc))
    if repository is None:
        from app.services.tw_kline_store import TWKlineRepository

        repository = TWKlineRepository()
    try:
        plan = build_plan(days, repository=repository, products=products)
    except ClosedDayError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    summary = {"mode": "apply" if args.apply else "dry-run", "plan": plan.as_dict()}
    exit_code = 0
    if args.apply:
        if importer is None:
            from app.services.taifex_history import TaifexHistoryImporter

            importer = TaifexHistoryImporter(repository=repository, delay=args.delay)
        result = apply_plan(plan, repository=repository, importer=importer)
        summary["result"] = result
        exit_code = 1 if result["failed"] else 0
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(run())
