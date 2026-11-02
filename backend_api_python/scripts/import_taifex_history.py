"""Import TAIFEX futures history into ``qd_tw_klines`` (``source=taifex``).

Manual history import from the TAIFEX website. The day-to-day data comes from
the TAIFEX OpenAPI through Celery Beat (``taifex-daily-collect`` for 1D bars,
``taifex-tick-backfill`` for 1m bars), but the OpenAPI only serves the latest
trading day; use this script for anything older (years of daily reports, tick
files of earlier days).

Daily bars (downloads the TAIFEX daily report month by month, 1 s apart):
    python scripts/import_taifex_history.py --daily --from 2020-01-01 --to 2026-10-31 --products TX,MTX,TMF

Tick files already downloaded from TAIFEX (``Daily_YYYY_MM_DD`` .csv or .zip,
a file or a directory), aggregated to 1m bars:
    python scripts/import_taifex_history.py --ticks ~/Downloads/taifex/

Download and import the tick files of given trading days (last 30 days only):
    python scripts/import_taifex_history.py --tick-days 2026-10-29,2026-10-30

Every write is an upsert that never replaces a ``source=fubon`` bar, so re-running
is safe; failed months are listed at the end and can simply be re-run.
Run inside the backend environment (same database settings as the API).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("SKIP_STARTUP_HOOKS", "1")

from app.services.taifex_history import (  # noqa: E402
    DEFAULT_PRODUCTS,
    ImportReport,
    TaifexHistoryImporter,
    parse_products,
)

_TICK_SUFFIXES = (".csv", ".zip", ".rpt", ".txt")


def tick_files(paths: Sequence[str]) -> List[Path]:
    """Expand files and directories into a sorted list of tick files."""
    out: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            out.extend(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in _TICK_SUFFIXES)
        elif path.is_file():
            out.append(path)
        else:
            raise FileNotFoundError(str(path))
    return sorted(set(out))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import TAIFEX futures history into qd_tw_klines from the TAIFEX website. "
            "The scheduled jobs (taifex-daily-collect, taifex-tick-backfill) already store "
            "the latest trading day from the TAIFEX OpenAPI; use this script for older history."
        ),
    )
    parser.add_argument("--daily", action="store_true", help="download and import daily bars")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, help="first date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end", type=date.fromisoformat, help="last date (YYYY-MM-DD)")
    parser.add_argument(
        "--products", default=",".join(DEFAULT_PRODUCTS),
        help="TAIFEX (TX,MTX,TMF) or Fubon (TXF,MXF) product codes",
    )
    parser.add_argument("--ticks", nargs="+", metavar="PATH", help="downloaded tick files or directories")
    parser.add_argument("--tick-days", help="comma-separated trading days whose tick files to download")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between TAIFEX requests")
    return parser


def run(argv: Optional[Sequence[str]] = None, importer: Optional[TaifexHistoryImporter] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.daily or args.ticks or args.tick_days):
        parser.error("choose --daily, --ticks or --tick-days")
    if args.daily and not (args.start and args.end):
        parser.error("--daily needs --from and --to")
    if args.daily and args.end < args.start:
        parser.error("--to must not be earlier than --from")
    try:
        products = parse_products(args.products)
    except ValueError as exc:
        parser.error(str(exc))
    importer = importer or TaifexHistoryImporter(delay=args.delay)

    summary = {}
    exit_code = 0
    if args.daily:
        report = importer.import_daily(args.start, args.end, products)
        summary["daily"] = report.as_dict()
        exit_code = 1 if report.failed else exit_code
    if args.ticks:
        report = ImportReport()
        for path in tick_files(args.ticks):
            try:
                importer.import_tick_bytes(path.read_bytes(), products=products, report=report)
            except Exception as exc:  # report the file and continue with the rest
                report.failed.append({"chunk": str(path), "error": str(exc)[:300]})
        summary["ticks"] = report.as_dict()
        exit_code = 1 if report.failed else exit_code
    if args.tick_days:
        report = ImportReport()
        for text in args.tick_days.split(","):
            if not text.strip():
                continue
            day = date.fromisoformat(text.strip())
            try:
                partial = importer.import_tick_day(day, products)
                for key, count in partial.written.items():
                    report.written[key] = report.written.get(key, 0) + count
            except Exception as exc:
                report.failed.append({"chunk": day.isoformat(), "error": str(exc)[:300]})
        summary["tick_days"] = report.as_dict()
        exit_code = 1 if report.failed else exit_code
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(run())
