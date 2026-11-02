"""Import TAIFEX option daily history into ``qd_tw_option_daily`` (issue #67).

Download the daily option report month by month (1 s between requests) and the
final settlement prices of the same period:
    python scripts/import_taifex_options_history.py --from 2020-01-01 --to 2026-10-31 --products TXO

Files already downloaded from TAIFEX (Big5 or UTF-8 CSV, or zip):
    python scripts/import_taifex_options_history.py --daily-file ~/Downloads/OptionsDaily_2026_10.csv
    python scripts/import_taifex_options_history.py --settlement-file ~/Downloads/final_settlement.csv

Every write is an upsert, so re-running is safe; failed months are listed at the
end and can simply be re-run. Run inside the backend environment (same database
settings as the API).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("SKIP_STARTUP_HOOKS", "1")

from app.services.taifex_options_history import (  # noqa: E402
    DEFAULT_OPTION_PRODUCTS,
    OptionImportReport,
    TaifexOptionsImporter,
    parse_option_products,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import TAIFEX option daily history and final settlement prices.")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, help="first date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end", type=date.fromisoformat, help="last date (YYYY-MM-DD)")
    parser.add_argument("--products", default=",".join(DEFAULT_OPTION_PRODUCTS), help="TAIFEX option products")
    parser.add_argument("--skip-settlements", action="store_true", help="do not download final settlement prices")
    parser.add_argument("--daily-file", nargs="+", metavar="PATH", help="downloaded daily option report files")
    parser.add_argument("--settlement-file", nargs="+", metavar="PATH", help="downloaded final settlement files")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between TAIFEX requests")
    return parser


def _import_files(paths: Sequence[str], handler, report: OptionImportReport) -> None:
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            handler(path.read_bytes(), report=report)
        except Exception as exc:  # report the file and continue with the rest
            report.failed.append({"chunk": str(path), "error": str(exc)[:300]})


def run(argv: Optional[Sequence[str]] = None, importer: Optional[TaifexOptionsImporter] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    download = bool(args.start or args.end)
    if not (download or args.daily_file or args.settlement_file):
        parser.error("choose --from/--to, --daily-file or --settlement-file")
    if download and not (args.start and args.end):
        parser.error("downloading needs both --from and --to")
    if download and args.end < args.start:
        parser.error("--to must not be earlier than --from")
    try:
        products = parse_option_products(args.products)
    except ValueError as exc:
        parser.error(str(exc))
    if not products:
        parser.error("--products is empty")
    importer = importer or TaifexOptionsImporter(delay=args.delay)

    summary = {}
    report = OptionImportReport()
    if download:
        report = importer.import_daily(args.start, args.end, products)
        if not args.skip_settlements:
            importer.import_settlements(args.start, args.end, products, report=report)
    if args.daily_file:
        _import_files(args.daily_file, lambda data, report: importer.import_daily_bytes(
            data, products=products, report=report), report)
    if args.settlement_file:
        _import_files(args.settlement_file, lambda data, report: importer.import_settlement_bytes(
            data, products=products, report=report), report)
    summary["options"] = report.as_dict()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(run())
