# Fubon Market Data Sidecar

[中文說明](README_CN.md)

A small, **read-only** HTTP service that logs in to Fubon Neo (`fubon_neo` SDK) and serves
Taiwan stock, index, futures and options market data to the QuantDinger backend.

`fubon_neo` only ships wheels for macOS arm64 and Linux x86_64, so it cannot be installed in
QuantDinger's Linux arm64 backend container on Apple Silicon. The sidecar runs as its own
`linux/amd64` container (Rosetta on Apple Silicon) and the backend talks to it over HTTP only.

The sidecar has **no order, cancel, account or position endpoints**, and the SDK object is
never exposed outside the service layer.

## 1. Download the SDK wheel

1. Download the Fubon Neo Python SDK from Fubon's official API site (Fubon Securities
   "新一代 API" download page). Use the **Linux x86_64** wheel, for example
   `fubon_neo-2.2.8-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`.
2. Put it in `fubon_sidecar/vendor/`. The directory is git-ignored; never commit the wheel.

The image pins `fugle-marketdata==2.4.1`. From 2.5.0 on, `base_url.with_version()` rejects the
`/v1.0` suffix that `fubon_neo` 2.2.8 hard-codes, and the realtime client fails with
`TypeError`. Do not upgrade it without verifying a newer `fubon_neo`.

## 2. Place the certificate

Copy the Fubon login certificate (`.pfx`) into a host directory outside version control, or
into `fubon_sidecar/certs/` (git-ignored). The directory is mounted read-only at `/run/fubon`.

## 3. Configure `.env`

Set these in the project-root `.env` (placeholders are in `.env.example`):

| Variable | Required | Description |
|---|---|---|
| `FUBON_ID` | yes | Fubon login ID |
| `FUBON_PASSWORD` | unless `FUBON_API_KEY` is set | Login password |
| `FUBON_API_KEY` | no | When set, the sidecar logs in with `sdk.apikey_login` |
| `FUBON_CERT_DIR` | no | Host directory with the certificate (default `./fubon_sidecar/certs`) |
| `FUBON_CERT_PATH` | yes | Certificate path inside the container, e.g. `/run/fubon/cert.pfx` |
| `FUBON_CERT_PASSWORD` | yes | Certificate password |
| `FUBON_SIDECAR_TOKEN` | yes | Shared secret, at least 32 characters (`openssl rand -hex 32`) |
| `FUBON_REQUEST_TIMEOUT_SEC` | no | Timeout per Fubon request, default `10` |
| `FUBON_RATE_LIMIT_PER_SEC` | no | Maximum Fubon requests per second, default `5` |

The sidecar refuses to start when the token is shorter than 32 characters. A rejected login is
not retried (repeated wrong passwords can lock the Fubon account); fix the credentials and
restart the container. Network errors during login are retried with backoff. Logs never
contain the ID, passwords, API key or token.

## 4. Start

```bash
docker compose --profile taiwan up -d --build fubon-marketdata
docker compose --profile taiwan ps fubon-marketdata
```

The service is in the `taiwan` compose profile, so a plain `docker compose up -d` does not
start it. It publishes no host port; only containers on `quantdinger-network` can reach
`http://fubon-marketdata:8010`. The container healthcheck turns healthy once the SDK is logged
in and realtime market data is initialized.

## 5. Verify

The port is not published, so run the checks inside the sidecar container (the token is read
from the container environment and never typed on the command line):

```bash
docker compose --profile taiwan exec fubon-marketdata python -m fubon_sidecar.probe /health
```

Expected: `{"status": "ok", "logged_in": true, "realtime_ready": true, "sdk_version": "2.2.8"}`.
Then check real data:

```bash
P="docker compose --profile taiwan exec fubon-marketdata python -m fubon_sidecar.probe"
$P '/v1/stock/candles?symbol=2330&timeframe=D'
$P '/v1/stock/intraday/candles?symbol=IX0001&timeframe=1'
$P '/v1/futopt/tickers?type=FUTURE'          # find the TXF near-month symbol, e.g. TXFL6
$P '/v1/futopt/intraday/candles?symbol=TXFL6&timeframe=1&session=regular'
$P '/v1/futopt/intraday/candles?symbol=TXFL6&timeframe=1&session=afterhours'
```

## 6. Daily K-line accumulation

Fubon keeps stock minute bars for the latest 5 trading days and futures minute bars for the
current session only. The backend's `celery-beat` therefore schedules
`quantdinger.tasks.tw_kline_collect` (maintenance queue, every
`TW_KLINE_COLLECT_INTERVAL_SEC`, default 600 s) to store each session's 1m bars in the
PostgreSQL table `qd_tw_klines`:

| Market | Session (`session`) | Collected after (Asia/Taipei) | Retried until |
|---|---|---|---|
| `TWStock` | `regular` | 14:00 on the trading day | 08:00 on the 3rd following trading day |
| `TWFutures` | `regular` (day) | 13:50 on the trading day | 08:30 on the next trading day |
| `TWFutures` | `afterhours` (night) | 05:10 the next morning | 08:30 on the next trading day |

- Trading days come from the `exchange_calendars` `XTAI` calendar; non-trading days are never fetched.
- Futures are stored per contract (`TXFH6`), for the near-month and next-month contracts by
  `settlementDate`, never as a continuous code.
- What to collect: `TW_KLINE_COLLECT_SYMBOLS` (`Market:Symbol` CSV, default
  `TWStock:IX0001,TWStock:2330.TW,TWStock:0050.TW,TWFutures:TXF,TWFutures:MXF`) plus every
  `TWStock` / `TWFutures` symbol on any user watchlist. `TW_KLINE_COLLECT_ENABLED=false` turns the
  job off; it also skips itself while `FUBON_SIDECAR_TOKEN` is empty.
- Options are not collected by default (there are too many contracts): option contracts on a
  watchlist and option products such as `TWFutures:TXO` are skipped. To keep a few, list the
  contracts in `TW_KLINE_COLLECT_SYMBOLS`, e.g. `TWFutures:TXO23000H6,TWFutures:TXO23000T6`.
- Each (symbol, session, trading day) outcome is written to `qd_tw_kline_collect_status`:
  `success`, `failed` (retried by the next run) or `missed` (the retry window closed; logged as a
  `TW K-line gap` warning). Find gaps with
  `SELECT * FROM qd_tw_kline_collect_status WHERE status <> 'success' ORDER BY trade_date DESC;`
- Writes are upserts on `(market, symbol, session, timeframe, bar_time)`, so reruns are idempotent.
  A `fubon` bar is never replaced by a later `taifex` import of the same bar.

**When the data starts:** minute history begins on the first trading day the job runs. The first
run also stores whatever the latest-5-trading-days stock history still holds; earlier days are
missing until the TAIFEX history import. Strategies read `TWStock` minute and hourly bars from the
table first and fill the most recent days from the sidecar, and the backtest data-coverage answer
reports the earliest stored bar.

## 7. TAIFEX futures history import

Fubon serves no futures history, so past `TWFutures` bars come from TAIFEX public data and are
written to the same `qd_tw_klines` table with `source=taifex`. Run the import from
`backend_api_python/` with the backend's database settings:

```bash
# Daily bars (TAIFEX daily report, one request per product and month, 1 s apart)
python scripts/import_taifex_history.py --daily --from 2020-01-01 --to 2026-10-31 --products TX,MTX,TMF
# 1m bars from tick files already downloaded from TAIFEX (Daily_YYYY_MM_DD .csv/.zip, file or directory)
python scripts/import_taifex_history.py --ticks ~/Downloads/taifex/
# Download and import the tick files of given trading days (TAIFEX keeps the last 30 days)
python scripts/import_taifex_history.py --tick-days 2026-10-29,2026-10-30
```

- **Product codes:** TAIFEX `TX`, `MTX`, `TMF`, `TE`, `TF`, `ZEF`, `ZFF` are stored as the Fubon codes
  `TXF`, `MXF`, `TMF`, `EXF`, `FXF`, `ZEF`, `ZFF` (`--products` accepts either form). Bars are
  stored per contract month (`TXFH6`); weekly contracts (`202605W4`) and spreads are skipped.
- **Daily report → `timeframe=1D`:** one bar per contract, session and trading day, labelled at
  Asia/Taipei midnight. A `一般` row is the day session; a `盤後` row of date D is the night session
  that opened at 15:00 on D and belongs to the next trading day. Months with no data are listed as
  `empty`, failed months as `failed` (the command exits 1); re-run the same command to retry.
- **Tick files → `timeframe=1m`:** trades are bucketed by minute; 成交數量(B+S) counts both sides,
  so volume is quantity / 2 contracts. The session follows the minute (day 08:30-14:00, night
  14:50-05:10). Files may be Big5 or UTF-8.
- **Idempotent:** every write is an upsert, and a bar that already has `source=fubon` is never
  replaced, so imports can be re-run at any time.
- **Division of work:** the TAIFEX OpenAPI only serves the latest trading day, which the two Celery
  Beat jobs below collect automatically. This script keeps the website downloads for manual imports
  outside that range (years of daily bars, tick files of earlier days).
- **Daily bars:** `quantdinger.tasks.taifex_daily_collect` (maintenance queue, every
  `TAIFEX_DAILY_COLLECT_INTERVAL_SEC`, default 1800 s) reads the TAIFEX OpenAPI
  `DailyMarketReportFut` and stores `TAIFEX_DAILY_COLLECT_PRODUCTS` (default `TX,MTX`) as
  `source=taifex` 1D bars. The report is published at about 06:35 Asia/Taipei the morning after a
  trading day, so the task does nothing before `TAIFEX_DAILY_COLLECT_NOT_BEFORE` (default `07:30`,
  Asia/Taipei); once the last 5 trading days are collected each run costs one status query. Each day
  is recorded in `qd_tw_kline_collect_status` as symbol `TAIFEX_DAILY`; a day the OpenAPI stopped
  serving before it was collected is `missed` and needs `--daily`. After a run that had pending days,
  the latest `source=taifex` day-session 1D bar in `qd_tw_klines` is compared with the latest trading
  day due by the XTAI calendar, and a `TAIFEX daily bars are stale` warning is logged when it is
  behind. `TAIFEX_DAILY_COLLECT_ENABLED=false` turns it off; it also skips itself while
  `FUBON_SIDECAR_TOKEN` is empty.
- **Daily gap fill:** `quantdinger.tasks.taifex_tick_backfill` (maintenance queue, every
  `TAIFEX_TICK_BACKFILL_INTERVAL_SEC`, default 3600 s) imports the trades of the last 5
  trading days before today for `TAIFEX_TICK_BACKFILL_PRODUCTS` (default `TX,MTX`) and adds the
  minutes the Fubon accumulation missed. The trading day currently served by the TAIFEX OpenAPI
  (`TimeAndSalesData`, probed first) is streamed from the OpenAPI; other days use the website's
  `Daily_YYYY_MM_DD.zip` (kept about 30 days). Days older than 30 days that are still missing are
  recorded as `missed` with a warning; import a kept tick file with `--ticks` for those. Each day
  is recorded in `qd_tw_kline_collect_status` as symbol `TAIFEX_TICKS`, its status text starting
  with `source=openapi` or `source=web_zip`; failed days (not published yet) are retried by the
  next run.
  `TAIFEX_TICK_BACKFILL_ENABLED=false` turns it off; it also skips itself while
  `FUBON_SIDECAR_TOKEN` is empty.

**Backtest range after an import:**

| Timeframe | Data source | Reaches back to |
|---|---|---|
| `1D`, `1W` | 1m bars where stored, otherwise the TAIFEX daily bars (imported or collected daily) | The `--daily --from` date (TAIFEX daily reports go back many years) |
| `1m` … `4H` | Stored 1m bars only (Fubon accumulation + TAIFEX tick files) | About 30 trading days before the first tick import, then continuous |

Continuous codes (`TXF`) pick the near month of each day from the stored contracts, so import every
month that was the near month (the daily report includes all listed months). The data-coverage
answer for `TWFutures:TXF` reports the first daily bar for `1D`/`1W` and the first 1m bar for
shorter timeframes. A single `1D`/`1W` backtest still spans at most 2 years.

## HTTP API (v1)

Every request, including `/health`, must send `X-Sidecar-Token: <FUBON_SIDECAR_TOKEN>`;
otherwise the response is `401 {"code": "unauthorized"}`. All timestamps are unix seconds
(UTC). Contract dates in ticker lists (`endDate`, `settlementDate`) stay `YYYY-MM-DD`.

| Endpoint | Fubon SDK call | Notes |
|---|---|---|
| `GET /health` | — | `{status, logged_in, realtime_ready, sdk_version}`; `status` is `ok`, `starting` or `error` |
| `GET /v1/stock/candles?symbol&timeframe&from&to` | `stock.historical.candles` | `D`/`W`/`M` accept a `from`/`to` range of at most one year (`YYYY-MM-DD` or unix seconds); `1`/`5`/`10`/`15`/`30`/`60` return only the latest 5 trading days and reject `from`/`to` |
| `GET /v1/stock/intraday/candles?symbol&timeframe` | `stock.intraday.candles` | Today's minute bars, indices included (e.g. `IX0001`) |
| `GET /v1/stock/quote?symbol` | `stock.intraday.quote` | Last price, change, OHLC, previous close, name |
| `GET /v1/stock/tickers?type&exchange&market` | `stock.intraday.tickers` | `type` `EQUITY`/`INDEX`/`WARRANT`/`ODDLOT`; `exchange` `TWSE`/`TPEx`; optional board `market` `TSE`/`OTC`/`ESB`/`TIB`/`PSB`; rows are `{symbol, name}` |
| `GET /v1/futopt/intraday/candles?symbol&timeframe&session` | `futopt.intraday.candles` | `session` `regular` (day, default) or `afterhours` (night) |
| `GET /v1/futopt/quote?symbol&session` | `futopt.intraday.quote` | Last price, `previousClose`, … |
| `GET /v1/futopt/tickers?type` | `futopt.intraday.tickers` | `type` `FUTURE`/`OPTION`; includes `symbol`, `name`, `endDate`, `settlementDate` |

Stock ticker list (`/v1/stock/tickers`): Fubon documents the filters `type`, `exchange`,
`market`, `industry`, `isNormal`, `isAttention`, `isDisposition` and `isHalted`; each row carries
only `symbol` and `name`, so the board is chosen with the `market` filter. The sidecar forwards
`market` and echoes it in the response (`{type, exchange, market, tickers}`). The board codes are
`TSE` listed, `OTC` over-the-counter, `TIB` Taiwan Innovation Board, and the emerging boards
`ESB` (興櫃一般板) and `PSB` (興櫃戰略新板). There is no filter or field for full-delivery
(全額交割) stocks; `isNormal` only separates attention, disposition and halted stocks.

The backend TWStock catalog requests `EQUITY` for `TWSE`+`TSE`, `TWSE`+`TIB` and `TPEx`+`OTC`
only, so emerging-board codes stay out of the catalog. A complete listing (stocks from both
exchanges, at least 70% of the currently active rows) marks absent TWStock codes inactive;
a failing or truncated sidecar response deactivates nothing.

Candle responses are `{symbol, timeframe, session?, candles: [{time, open, high, low, close, volume}]}`
sorted by time ascending. Fubon returns the bar time in a `date` field (an ISO string such as
`2026-07-06T08:45:00.000+08:00`, or `YYYY-MM-DD` for daily bars, which is treated as midnight
Asia/Taipei), not in `time` as documented.

Quote responses are `{symbol, name, lastPrice, change, changePercent, open, high, low,
previousClose, referencePrice, volume, time}` (+ `session` for futures/options).

Errors use `{code, message?}`:

| Status | `code` | When |
|---|---|---|
| 400 | `invalidRequest` | Missing or invalid parameters |
| 401 | `unauthorized` | Missing or wrong `X-Sidecar-Token` |
| 404 | `notFound` | Fubon has no data for the request |
| 429 | `rateLimited` | The local per-second cap could not be met within the request timeout |
| 502 | `upstreamError` | The SDK raised or timed out (message contains no secrets) |
| 503 | `notReady` | Not logged in or realtime market data not initialized yet |

## Development

```bash
cd fubon_sidecar
python -m pip install -e ".[dev]"
ruff check .
python -m pytest -q
```

Tests inject a fake SDK through the `sdk_factory` argument and never contact Fubon. CI
(`.github/workflows/fubon-sidecar-ci.yml`) runs only when `fubon_sidecar/**` changes and does not
install `fubon_neo`.
