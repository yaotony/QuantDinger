# Example session: BTC 15-minute ORB

A complete run of the [QuantDinger Strategy Lab skill](../SKILL.md), from a one-sentence request to a diagnosed result. Tool responses are abridged to the fields the agent uses. All numbers are illustrative; a real run reports what the server returns.

---

**User:** Design a BTC 15-minute ORB strategy for me and backtest it.

**Agent:** I'll check the environment first.

```text
check_environment()
```

```json
{
  "overall": "pass",
  "token": {"scopes": ["R", "W", "B"], "paper_only": true},
  "backtest_quota": {"limit": 2, "active": 0},
  "backtest_range_limits": {"Crypto": {"15m": 365}}
}
```

**Agent:** The token has research scopes only (R, W, B), it is paper-only, and both backtest slots are free. Here is the spec I'll test. Tell me if any default is wrong:

| Item | Value |
| --- | --- |
| Instrument | `Crypto:BTC/USDT@spot` (Binance-routed data) |
| Timeframe | 15m |
| Opening range | the first 4 bars (1 hour) after 00:00 UTC |
| Entry | long when a 15m bar closes above the range high, at most one entry per day |
| Exit | 1% stop loss, otherwise flat at the start of the next UTC day |
| Direction / size | long-only, 50% of equity, no leverage |
| Costs | backend defaults (0.1% commission, 0.05% slippage per side) |

**User:** Looks good.

**Agent:** I'll check data coverage. I plan to develop on 2025-01-01 to 2025-06-30 and keep 2025-07-01 to 2025-09-30 untouched for out-of-sample validation.

```text
get_data_coverage(market="Crypto", symbol="BTC/USDT", timeframe="15m",
                  start_date="2025-01-01", end_date="2025-09-30",
                  warmup_bars=96, exchange_id="binance")
```

```json
{
  "earliest_bar": {"status": "known", "time": "2017-08-17T04:00:00Z"},
  "limits": {"max_days": 365, "warmup_days": 2, "max_selected_days": 363, "warmup_fits": true},
  "evaluation": {"feasible": true, "range_check": {"ok": true}, "history_check": {"status": "ok"}}
}
```

**Agent:** Both windows fit (272 days plus 2 warmup days, against a 365-day limit). Next I'll read the authoring contract and templates.

```text
get_strategy_authoring_contract()
list_strategy_templates(limit=20)
```

**Agent:** I'll start from the breakout template and write the source:

```python
"""BTC 15m Opening Range Breakout
Long-only breakout of the first UTC hour; flat by the next UTC day.
"""

# @param range_bars int 4 Opening range length in 15m bars range=2:8:1
# @param stop_pct float 0.01 Stop-loss distance range=0.005:0.03:0.005
# @param target_pct float 0.5 Target portfolio weight range=0.1:0.95:0.05


def initialize(context):
    g.symbol = "Crypto:BTC/USDT@spot"
    context.set_universe([g.symbol])
    context.subscribe(frequency="15m", fields=["open", "high", "low", "close", "volume"])
    context.set_warmup(96)
    context.set_metadata(direction_mode="long_only")
    g.day = None
    g.bars_today = 0
    g.range_high = None
    g.range_low = None
    g.entered = False


def handle_data(context, data):
    range_bars = int(context.params.get("range_bars", 4))
    stop_pct = float(context.params.get("stop_pct", 0.01))
    target_pct = float(context.params.get("target_pct", 0.5))

    bars = get_history(1, "15m", ["high", "low", "close"], g.symbol)
    if len(bars) < 1:
        return
    high = float(bars["high"].iloc[-1])
    low = float(bars["low"].iloc[-1])
    close = float(bars["close"].iloc[-1])
    amount = float(get_position(g.symbol).amount or 0.0)

    day = context.current_dt.date()
    if g.day != day:
        g.day = day
        g.bars_today = 0
        g.range_high = None
        g.range_low = None
        g.entered = False
        if amount > 0:
            order_target_percent(g.symbol, 0.0, reason="orb_day_exit")
            return

    g.bars_today += 1
    if g.bars_today <= range_bars:
        g.range_high = high if g.range_high is None else max(g.range_high, high)
        g.range_low = low if g.range_low is None else min(g.range_low, low)
        return

    if not g.entered and amount <= 0 and close > g.range_high:
        order_target_percent(g.symbol, target_pct, reason="orb_breakout", stop_loss_pct=stop_pct)
        g.entered = True
```

```text
compile_strategy_code(code="<source above>")
```

```json
{"manifest": {"universe": ["Crypto:BTC/USDT@spot"], "frequencies": ["15m"],
              "drivingFrequency": "15m", "warmupBars": 96,
              "directionMode": "long_only", "leverageAllowed": false}}
```

**Agent:** It compiles, and the manifest matches the spec (warmup 96 bars, as checked). Saving it:

```text
save_strategy_source(name="btc-orb-15m", code="<source above>",
                     description="ORB v1: first-hour breakout, 1% stop",
                     idempotency_key="lab-orb-v1-save")
```

```json
{"id": 12, "version_id": 1}
```

```text
submit_backtest(code="<source above>", start_date="2025-01-01", end_date="2025-06-30",
                initial_capital=10000, params={}, idempotency_key="lab-orb-v1-bt")
```

```json
{"job_id": "job-1", "status": "queued"}
```

```text
wait_for_job(job_id="job-1")
```

```json
{
  "status": "succeeded",
  "result": {
    "runId": 301, "resultStatus": "completed_trades",
    "totalReturn": -6.8, "benchmarkTotalReturn": 14.9, "maxDrawdown": 9.4,
    "sharpeRatio": -0.71, "totalTrades": 112, "totalExecutions": 224,
    "winRate": 41.1, "profitLossRatio": 1.31, "profitFactor": 0.91,
    "totalCommission": 1120.0, "totalProfit": -680.0,
    "executionAssumptions": {"commission": 0.001, "slippage": 0.0005, "fundingMode": "not_modeled"}
  }
}
```

**Agent:** Iteration 1 lost 6.8% while holding BTC gained 14.9%. Diagnosis:

- **Break-even:** with a payoff of 1.31, the strategy needs a 43.3% win rate (`1 / (1 + 1.31)`); it wins 41.1% of the time, so it loses slightly before anything else.
- **Costs dominate:** 112 round trips at about 5,000 USDT notional cost about 1,120 USDT in commission plus about 560 USDT in slippage, about 16.8% of starting capital. Before costs the strategy made about +10%; costs turned that into -6.8%.
- **Cause:** most entries come on narrow opening ranges, where a 1% stop and a 0.3% round-trip cost are large relative to the typical move.

Hypothesis for iteration 2: skip days whose opening range is narrower than 0.4% of price, because a 0.3% round-trip cost needs a larger move to pay off. This is one change: a new filter with a value chosen from the cost arithmetic, not from a sweep.

```text
save_strategy_source(source_id=12, name="btc-orb-15m", code="<source with min_range_pct filter>",
                     description="ORB v2: skip opening ranges narrower than 0.4%",
                     idempotency_key="lab-orb-v2-save")
submit_backtest(code="<v2 source>", start_date="2025-01-01", end_date="2025-06-30",
                initial_capital=10000, params={}, idempotency_key="lab-orb-v2-bt")
wait_for_job(job_id="job-2")
```

```json
{"status": "succeeded",
 "result": {"runId": 302, "totalReturn": 3.9, "maxDrawdown": 4.1, "sharpeRatio": 0.9,
            "totalTrades": 46, "winRate": 47.8, "profitLossRatio": 1.42,
            "profitFactor": 1.3, "totalCommission": 460.0}}
```

**Agent:** v2 now clears break-even (47.8% versus 41.3% needed) and trades 46 times instead of 112. Because I changed the strategy, I'll validate it once on the untouched out-of-sample window and once with doubled costs.

```text
submit_backtest(code="<v2 source>", start_date="2025-07-01", end_date="2025-09-30",
                initial_capital=10000, params={}, idempotency_key="lab-orb-v2-oos")
submit_backtest(code="<v2 source>", start_date="2025-01-01", end_date="2025-06-30",
                initial_capital=10000, commission=0.002, slippage=0.001, params={},
                idempotency_key="lab-orb-v2-cost2x")
wait_for_job(job_id="job-3")
wait_for_job(job_id="job-4")
```

```json
{"job-3": {"runId": 303, "totalReturn": 0.6, "maxDrawdown": 3.2, "totalTrades": 19,
           "winRate": 42.1, "benchmarkTotalReturn": 8.1},
 "job-4": {"runId": 304, "totalReturn": -0.7, "totalTrades": 46}}
```

**Agent:** Result report:

| # | Change | Version | Window | Costs | Trades | Return % | Max DD % | Win % | runId |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | baseline ORB | 1 | 2025-01-01 to 2025-06-30 | default | 112 | -6.8 | 9.4 | 41.1 | 301 |
| 2 | skip opening ranges < 0.4% | 2 | 2025-01-01 to 2025-06-30 | default | 46 | 3.9 | 4.1 | 47.8 | 302 |
| 3 | v2 out-of-sample | 2 | 2025-07-01 to 2025-09-30 | default | 19 | 0.6 | 3.2 | 42.1 | 303 |
| 4 | v2 doubled costs | 2 | 2025-01-01 to 2025-06-30 | 2x | 46 | -0.7 | n/a | n/a | 304 |

- **Sample size:** 46 in-sample trades and 19 out-of-sample trades, four variants tested in total. 19 trades are too few to separate skill from luck.
- **Market conditions:** the in-sample half-year rose overall (BTC +14.9%) with a sharp spring drawdown; the out-of-sample quarter rose more steadily (+8.1%). Neither window contains a long bear market or range-bound regime, so the result says nothing about those.
- **Costs:** 0.1% commission and 0.05% slippage per side; Crypto funding is not modeled. Doubling costs turns v2 negative, so the edge is thin.
- **Diagnosis:** the opening-range filter fixed the cost problem in-sample, but out-of-sample the win rate fell back to 42.1%, barely above the 41.3% break-even. Both versions trailed buy-and-hold.
- **Verdict:** v2 is not robust enough to deploy. Source 12 (version 2) is saved for further research.

Possible next hypotheses (one per iteration): require the breakout bar's volume to exceed the opening-range average, or test on a period that includes a range-bound regime. Should I continue with one of these?

**User:** Not now. If it gets better later, can you deploy it?

**Agent:** When a version passes out-of-sample and cost checks, I can create the deployment in `signal` mode with `create_strategy(execution_mode="signal", ...)`. It is created stopped and only produces signals. Starting it, connecting an exchange account, and any move to live trading stay with you in the web UI.
