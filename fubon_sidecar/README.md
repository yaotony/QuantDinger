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
| `GET /v1/stock/tickers?type&exchange` | `stock.intraday.tickers` | `type` `EQUITY`/`INDEX`/`WARRANT`/`ODDLOT`; `exchange` `TWSE`/`TPEx` |
| `GET /v1/futopt/intraday/candles?symbol&timeframe&session` | `futopt.intraday.candles` | `session` `regular` (day, default) or `afterhours` (night) |
| `GET /v1/futopt/quote?symbol&session` | `futopt.intraday.quote` | Last price, `previousClose`, … |
| `GET /v1/futopt/tickers?type` | `futopt.intraday.tickers` | `type` `FUTURE`/`OPTION`; includes `symbol`, `name`, `endDate`, `settlementDate` |

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
