# Backtest Report: BTC trend \| test

Run #41 · created 2026-10-30T12:00:00 · engine strategy-v2-engine-test

## Summary

| Metric | Value |
| --- | --- |
| Period | 2024-01-01 – 2024-06-30 |
| Market | Crypto |
| Symbol | BTC/USDT |
| Timeframe | 4H |
| Initial capital | 10,000.00 |
| Final equity | 10,195.00 |
| Total return | 1.95% |
| Annualized return | 6.21% |
| Buy-and-hold return (BTC/USDT) | 4.50% |
| Excess return vs buy-and-hold | -2.55% |
| Max drawdown | -3.42% |
| Sharpe ratio | 0.84 |
| Sortino ratio | 1.17 |
| Calmar ratio | 1.82 |

## Cost Breakdown

| Metric | Value |
| --- | --- |
| Commission | 33.40 |
| Slippage | 16.50 |
| Funding | Not modeled (not included) |
| Total trading cost | 49.90 |
| Gross profit before costs | 465.30 |
| Gross loss before costs | 220.40 |
| Cost / gross profit | 10.72% |
| Cost / gross loss | 22.64% |

Commission and slippage come from the trade diagnostics over all closed trades; total commission over all executions is 33.40.

## Trade Diagnostics

| Metric | Value |
| --- | --- |
| Closed trades | 4 |
| Win rate | 50.00% |
| Profit factor | 1.80 |
| Average R | -0.10 (3 trades with a stop loss) |
| Expectancy per trade before costs | 61.22 |
| Expectancy per trade after costs | 48.75 |

### Holding Time Distribution

| Holding time | Trades | Share |
| --- | --- | --- |
| Up to 1 hour | 1 | 25.0% |
| 1 hour to 1 day | 0 | 0.0% |
| 1 to 7 days | 2 | 50.0% |
| Over 7 days | 1 | 25.0% |

| Metric | Value |
| --- | --- |
| Shortest | 45m |
| Median | 2d 21h |
| Average | 4d 4h |
| Longest | 11d |

### MAE / MFE

| Metric | Value |
| --- | --- |
| Average MAE | 2.24% |
| Average MFE | 3.41% |
| Worst MAE | 5.12% |
| Best MFE | 6.67% |
| Average MAE (R) | 1.21R |
| Average MFE (R) | 1.15R |

## Execution Assumptions

| Metric | Value |
| --- | --- |
| Commission rate | 0.1% |
| Slippage rate | 0.05% |
| Leverage | 1.00x |
| Fill rule | `scheduled_current_open_or_signal_next_open` |
| Intrabar mode | `conservative` |
| Bar close policy | `closed_bars_only` |
| Stop/take-profit rule | `gap_open_then_intrabar_trigger` |
| Funding | Not modeled (not included) |
| Data kind | `market` |

### Model Limitations

- Orders fill at bar prices with a fixed slippage rate; order-book depth, queue position and partial fills are not simulated.
- Intrabar paths are unknown; stop and take-profit triggers inside one bar are resolved conservatively.
- Funding payments, borrow costs and dividends are not included unless listed above.
- Past results on historical data do not guarantee future performance.

## Warnings

- Buy-and-hold benchmark covers 98.2% of the backtest bars.
- Funding fees are not modeled; perpetual-futures results may be overstated or understated.

## Strategy Version

| Metric | Value |
| --- | --- |
| Strategy name | BTC trend \| test |
| Source ID | 12 |
| Source version | 3 |
| Strategy ID | No data |
| Code hash | `abc123` |
| Engine version | `strategy-api-v2` |
| Parameters | `{"fast": 10, "slow": 30}` |
