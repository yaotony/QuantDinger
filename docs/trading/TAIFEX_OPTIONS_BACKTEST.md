# TAIFEX Option History and Offline Option Backtester

[繁體中文](TAIFEX_OPTIONS_BACKTEST_CN.md)

> **Research only. Results must not be treated as live-tradable performance.**
> The backtester works from daily TAIFEX data without bid/ask quotes and
> approximates exchange margin. Every result carries `not_live_performance: true`.

Strategy API V2 has no option instrument type. Option contracts submitted to
`submit_backtest`, the backtest service, or paper/live deployments are still
rejected with `strategyV2.optionsBacktestUnsupported` (issue #59). The tools below
are separate: an importer for TAIFEX option daily history and a standalone
offline backtester for TXO monthly options. Neither is connected to Strategy API
V2, paper trading, or live trading.

## 1. Option daily history

### Tables

`qd_tw_option_daily` (one row per trading day, product, expiry, strike, right and session):

| Column | Meaning |
| --- | --- |
| `trade_date` | TAIFEX `交易日期` as published. An after-hours (`盤後`) row of date D is the night session that opened at 15:00 on D. |
| `product` | TAIFEX product code, e.g. `TXO` |
| `expiry` | Contract month `202608`, or week label such as `202608W1` |
| `strike` | Strike price |
| `option_right` | `call` (`買權`) or `put` (`賣權`) |
| `session` | `regular` (`一般`) or `afterhours` (`盤後`) |
| `symbol` | Option code from #59 (`TXO23000H6`) for monthly series; empty for weekly series |
| `open`, `high`, `low`, `close` | Prices; `NULL` on a day without trades (`-`) |
| `volume` | Contracts traded (0 when none) |
| `settlement_price` | Daily settlement price (`結算價`); `NULL` when TAIFEX publishes none |
| `open_interest` | Open interest (`未沖銷契約數`) |
| `source` | `taifex` |

`qd_tw_option_settlement` stores one final settlement price per `product` and
`contract_month` (`settlement_date`, `final_settlement_price`). TXO is a
cash-settled European option, so an expiring position cannot be settled without it.

Both tables are created by `migrations/init.sql` and the registered migration
`20261103_tw_option_daily.sql` (`CREATE TABLE IF NOT EXISTS`). They hold market
data shared by all tenants. Every write is an upsert on the unique key, so a
re-run never duplicates rows and a corrected value replaces the old one.

### Import

Run inside the backend environment (same database settings as the API):

```bash
cd backend_api_python
python scripts/import_taifex_options_history.py --from 2020-01-01 --to 2026-10-31 --products TXO
```

- The daily option report is downloaded one calendar month per request from
  `https://www.taifex.com.tw/cht/3/optDataDown`, 1 second apart (`--delay`).
- The final settlement table of the same period is downloaded from
  `https://www.taifex.com.tw/cht/5/fSPriceDown` (skip with `--skip-settlements`).
- A failed month is listed in the `failed` output and the exit code is 1; re-run
  the same command (or just that range) to fill it.
- Files downloaded by hand can be imported instead:
  `--daily-file <csv/zip ...>` and `--settlement-file <csv ...>`.
- Files may be Big5 (cp950) or UTF-8 (with or without BOM), plain or zipped.
- Columns are located by header name (`交易日期`, `契約`, `到期月份`, `履約價`,
  `買賣權`, `開盤價`, `最高價`, `最低價`, `收盤價`, `成交量`, `結算價`,
  `未沖銷契約…`, optional `交易時段`), not by position. Rows that cannot be parsed
  (unknown right, bad date, spread months, missing strike, truncated rows) are
  counted in `invalid_rows` and skipped.
- The settlement parser accepts a long table (`最後結算日, 契約, 契約月份, 最後結算價`)
  or a wide one with a column per product whose header names it
  (`臺指選擇權(TXO)`). A missing price (`-`) is never stored as zero.

**Data start.** The reference import starts on 2020-01-01. The importer accepts
any range; how far back TAIFEX serves the daily option report must be checked on
the TAIFEX site. The owner verifies the download endpoints and form fields, and
spot-checks imported days and final settlement prices against the TAIFEX website.

## 2. Offline backtester

Package: `backend_api_python/app/services/options_backtest/`. CLI:

```bash
cd backend_api_python
python scripts/run_options_backtest.py strategy.json --from 2024-01-01 --to 2025-12-31 \
    --cost-multiplier 2 --sample out --output result.json
```

The JSON result goes to `--output` (or stdout) and a short summary to stderr.
A missing final settlement price or daily settlement price stops the run with exit code 1.

### Scope of the first version

- Product: TXO monthly options only (weekly series are ignored).
- Structures: `long_call`, `long_put`, `short_call`, `short_put`,
  `bull_call_spread`, `bear_call_spread`, `bull_put_spread`, `bear_put_spread`,
  `iron_condor`. Legs are listed in strike order (an iron condor is buy put,
  sell put, sell call, buy call).
- Every position records its maximum expiry loss from the entry premiums
  (costs excluded); a net short call position is recorded as unlimited
  (`max_loss: null`, `max_loss_unlimited: true`).

### Strategy file

Explicit trades:

```json
{
  "name": "one-call",
  "config": {"initial_capital": 100000},
  "trades": [
    {"entry_date": "2026-08-03", "expiry": "202608", "structure": "long_call",
     "legs": [{"side": "buy", "right": "call", "strike": 23000}], "quantity": 1,
     "exit_date": null}
  ]
}
```

A monthly rule:

```json
{
  "name": "txo-bull-put",
  "config": {"initial_capital": 1000000, "costs": {"fee_per_contract": 25},
             "liquidity": {"min_volume": 20, "min_open_interest": 100}},
  "rule": {"structure": "bull_put_spread", "quantity": 1,
           "legs": [{"side": "buy", "right": "put", "strike_offset": -600},
                    {"side": "sell", "right": "put", "strike_offset": -300}],
           "entry_days_before_expiry": 30, "min_days_to_expiry": 7,
           "exit_days_before_expiry": 0}
}
```

- A leg has an absolute `strike` or a `strike_offset` in index points from the
  underlying implied by put-call parity on the entry day (`K + C - P` at the strike
  where `|C - P|` is smallest, using settlement prices), rounded to the nearest
  listed strike.
- Rule entries target the nearest expiry with at least `min_days_to_expiry`
  calendar days left, once the days left are at most `entry_days_before_expiry`.
  One position per expiry; a rejected entry is retried on the next trading day.
- An explicit trade is attempted once, on the first trading day on or after `entry_date`.
- `exit_date` / `exit_days_before_expiry` (0 = hold to expiry) close the position
  early. A blocked exit is retried every trading day up to the expiry day.

### Daily processing order

1. Scheduled exits.
2. New entries: liquidity filter, then margin check.
3. Positions whose expiry date has been reached are cash settled.
4. Every open position is marked at the day's settlement price; the equity curve
   and margin in use are recorded.

No position is skipped. A position that cannot be marked or settled stops the run
with an explicit error; positions still open at the end are listed in
`open_positions` and are not counted as trades.

### Fills and liquidity

- Reference price: the day's close (`fill_price: "close"`) or settlement price
  (`"settlement"`), plus `slippage_ticks` TXO ticks for a buy and minus for a sell
  (TXO tick: 0.1 below 10 points, 0.5 below 50, 1 below 500, 5 below 1,000, 10 above).
- There is no bid/ask in the daily data. A strike whose volume is below
  `min_volume` or open interest is below `min_open_interest` cannot be traded
  (entry rejected, or exit blocked and retried). Strikes without a reference or
  settlement price cannot be traded either.

### Costs

| Parameter | Default | Basis | Status |
| --- | --- | --- | --- |
| `multiplier` | 50 | TXO contract size NT$50 per index point | TAIFEX contract specification |
| `tax_rate` | 0.001 | Futures transaction tax on option premium, 0.1% per side | Futures Transaction Tax Act; owner to confirm |
| `settlement_tax_rate` | 0.001 | Tax on the cash-settled value at expiry (applied to both long and short in-the-money legs) | Assumption; owner to confirm |
| `fee_per_contract` | 25 | Broker commission incl. exchange/clearing fees, per contract per side | Broker-specific; owner to set |
| `settlement_fee_per_contract` | 25 | Broker fee per contract settled in the money | Broker-specific; owner to set |
| `slippage_ticks` | 1 | Research assumption | — |
| `cost_multiplier` | 1 | Scales fees, taxes and slippage (2 = double cost) | Research rule |

Taxes are not rounded to whole NT$ as brokers do (error below NT$1 per fill).

### Expiry

At the expiry date (TAIFEX last trading day, from #59's calendar), every leg is
cash settled at its intrinsic value against the final settlement price; an
out-of-the-money leg settles at zero without settlement costs. Both legs of a
spread settle on the same day. A missing final settlement price raises
`SettlementPriceMissing`; it is never treated as zero.

### Margin

| Structure | Margin per unit |
| --- | --- |
| Long legs, debit spreads | 0 (premium paid in full) |
| Short single | premium market value + max(A − out-of-money amount, B) |
| Credit vertical spread | strike width × 50 |
| Iron condor | wider spread width × 50 |

- `margin_a` / `margin_b` default to NT$85,000 / NT$43,000. TAIFEX revises A and B
  over time; one value is used for the whole run, so set them for the tested
  period. The owner must confirm current values against the TAIFEX announcement.
- The out-of-money amount uses the put-call parity underlying, not the TAIEX close;
  without an estimate it is taken as zero (higher margin).
- An entry is rejected when margin + debit + entry costs exceed equity minus margin
  already in use. Credit received is not counted towards the requirement.
- Margin is re-computed daily at settlement prices. When it exceeds equity, a
  `margin_shortfall` event is recorded; positions are not force-liquidated.

### Output

`metrics` and the flat `ledger` record contain at least: `total_return`,
`max_drawdown` (fraction of the running peak, starting at the initial capital),
`trades` (closed positions), `win_rate`, `total_cost`, `max_single_loss` (most
negative closed-trade P&L, 0 when none), `peak_margin`, `final_equity`,
`open_positions_at_end`, `cost_multiplier`, `sample` and `not_live_performance`.
The result also lists `trades`, `open_positions`, `equity_curve` (daily equity,
cash, margin) and `events` (entries, rejections with reasons, blocked exits,
expiries, margin shortfalls).

### Research rules

Run the in-sample and out-of-sample periods separately (`--sample in` / `--sample out`)
and repeat with `--cost-multiplier 2`. Record each `ledger` object in the research ledger.

## 3. Known errors and limits

- Daily data only: no bid/ask, no intraday path. Fills at close or settlement plus
  slippage can differ materially from executable prices, especially for
  far out-of-the-money strikes.
- Margin is an approximation (fixed A/B, parity underlying, no portfolio offsets
  beyond the listed structures, no intraday margin calls).
- Taxes and fees use the configured rates; actual broker charges and rounding differ.
- The underlying estimate ignores interest and dividends (at most about one month to expiry).
- Weekly options, stock options, intraday and tick-level backtests are out of scope.
