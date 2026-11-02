"""Run the offline TXO option backtester on ``qd_tw_option_daily`` (issue #67).

    python scripts/run_options_backtest.py strategy.json --from 2024-01-01 --to 2025-12-31 \
        --cost-multiplier 2 --sample out --output result.json

The JSON result goes to ``--output`` (or stdout); a short summary goes to stderr.
``result["ledger"]`` is the flat record for the research ledger. Results are
research estimates, not live-tradable performance. This tool is not connected to
Strategy API V2, ``submit_backtest``, paper or live trading.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("SKIP_STARTUP_HOOKS", "1")

from app.services.options_backtest import (  # noqa: E402
    OptionMarket,
    OptionsBacktestError,
    format_summary,
    run_backtest,
)

MarketLoader = Callable[[str, date, date], OptionMarket]


def load_market_from_db(product: str, start: date, end: date) -> OptionMarket:
    from app.services.tw_option_store import TWOptionRepository

    repository = TWOptionRepository()
    rows = repository.load_daily(product=product, start=start, end=end)
    settlements = repository.load_settlements(product=product)
    return OptionMarket(rows, settlements, product=product)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline TXO option backtest (research only).")
    parser.add_argument("strategy", help="strategy JSON file")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True, help="first date")
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True, help="last date")
    parser.add_argument("--cost-multiplier", type=float, default=None, help="scale every cost (2 = double cost)")
    parser.add_argument("--sample", default="", help="label stored in the ledger record, e.g. in / out")
    parser.add_argument("--output", help="write the JSON result to this file instead of stdout")
    return parser


def run(argv: Optional[Sequence[str]] = None, market_loader: Optional[MarketLoader] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--to must not be earlier than --from")
    try:
        strategy = json.loads(Path(args.strategy).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read strategy file: {exc}")
    product = str((strategy.get("config") or {}).get("product") or "TXO") if isinstance(strategy, dict) else "TXO"
    loader = market_loader or load_market_from_db
    try:
        market = loader(product, args.start, args.end)
        result = run_backtest(market, strategy, args.start, args.end,
                              cost_multiplier=args.cost_multiplier, sample=args.sample)
    except (OptionsBacktestError, ValueError) as exc:
        print(f"options backtest failed: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).expanduser().write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    print(format_summary(result), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
