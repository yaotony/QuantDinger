"""Keep the agent strategy-iteration guide aligned with backtest range policy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.backtest_limits import (
    backtest_range_policy,
    backtest_warmup_calendar_days,
)

DOCS_AGENT = Path(__file__).resolve().parents[2] / "docs" / "agent"
GUIDES = ("AGENT_STRATEGY_ITERATION.md", "AGENT_STRATEGY_ITERATION_CN.md")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
# Column order in the documented table: USStock, Forex, other markets.
MARKET_COLUMNS = ("USStock", "Forex", "Crypto")
ROW_RE = re.compile(r"^\|\s*`(\d+[mhdw])`\s*\|(.+)\|\s*$")


def _range_table(text: str) -> dict[str, list[int]]:
    rows: dict[str, list[int]] = {}
    for line in text.splitlines():
        match = ROW_RE.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group(2).split("|")]
        days = [re.match(r"(\d+)", cell) for cell in cells]
        if len(cells) == len(MARKET_COLUMNS) and all(days):
            rows[match.group(1)] = [int(item.group(1)) for item in days]
    return rows


@pytest.mark.parametrize("guide", GUIDES)
def test_documented_range_limits_match_policy(guide):
    rows = _range_table((DOCS_AGENT / guide).read_text(encoding="utf-8"))

    assert set(rows) == set(TIMEFRAMES)
    for timeframe, documented in rows.items():
        expected = [backtest_range_policy(market, timeframe).max_days for market in MARKET_COLUMNS]
        assert documented == expected, f"{guide} {timeframe}"


@pytest.mark.parametrize("market", ["HKStock", "AStock"])
def test_other_market_column_matches_default_policy(market):
    for timeframe in TIMEFRAMES:
        assert (
            backtest_range_policy(market, timeframe).max_days
            == backtest_range_policy("Crypto", timeframe).max_days
        )


def test_documented_daily_warmup_example():
    warmup_days = backtest_warmup_calendar_days("1d", 200)

    assert warmup_days == 378
    assert backtest_range_policy("Crypto", "1d").max_days - warmup_days == 717
    assert backtest_range_policy("USStock", "1d").max_days - warmup_days == 3272


@pytest.mark.parametrize("guide", GUIDES)
def test_guide_names_documented_error_codes(guide):
    text = (DOCS_AGENT / guide).read_text(encoding="utf-8")

    for code in ("strategyV2.backtestRangeLimit", "strategyV2.insufficientWarmupData"):
        assert code in text
