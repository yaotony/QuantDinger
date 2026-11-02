# 富邦行情 Sidecar

[English](README.md)

一個**只讀**的小型 HTTP 服務：登入富邦新一代 API（`fubon_neo` SDK），把台灣股票、指數、期貨、
選擇權行情提供給 QuantDinger 後端。

`fubon_neo` 只有 macOS arm64 與 Linux x86_64 的 wheel，裝不進 Apple Silicon 上 QuantDinger 的
Linux arm64 後端容器。因此 sidecar 以獨立的 `linux/amd64` 容器執行（Apple Silicon 透過 Rosetta），
後端只透過 HTTP 取資料。

sidecar **沒有任何下單、刪單、帳務、部位端點**，SDK 物件也不會暴露到服務層以外。

## 1. 下載 SDK wheel

1. 從富邦證券官網「新一代 API」下載頁取得 Python SDK，選 **Linux x86_64** 版本，例如
   `fubon_neo-2.2.8-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`。
2. 放到 `fubon_sidecar/vendor/`。此目錄已加入 `.gitignore`，wheel 不可 commit。

image 固定使用 `fugle-marketdata==2.4.1`。2.5.0 起 `base_url.with_version()` 會拒絕
`fubon_neo` 2.2.8 寫死的 `/v1.0`，行情 client 會丟 `TypeError`。未驗證新版 `fubon_neo` 前不要升級。

## 2. 放置憑證

把富邦登入憑證（`.pfx`）放在不受版控的主機目錄，或放進 `fubon_sidecar/certs/`（已 git-ignore）。
該目錄會以唯讀方式掛載到容器的 `/run/fubon`。

## 3. 設定 `.env`

在專案根目錄 `.env` 設定下列變數（`.env.example` 已有空白佔位）：

| 變數 | 必填 | 說明 |
|---|---|---|
| `FUBON_ID` | 是 | 富邦登入帳號（身分證字號） |
| `FUBON_PASSWORD` | 未設 `FUBON_API_KEY` 時必填 | 登入密碼 |
| `FUBON_API_KEY` | 否 | 有設定時改用 `sdk.apikey_login` 登入 |
| `FUBON_CERT_DIR` | 否 | 放憑證的主機目錄（預設 `./fubon_sidecar/certs`） |
| `FUBON_CERT_PATH` | 是 | 容器內的憑證路徑，例如 `/run/fubon/cert.pfx` |
| `FUBON_CERT_PASSWORD` | 是 | 憑證密碼 |
| `FUBON_SIDECAR_TOKEN` | 是 | 後端與 sidecar 的共享密鑰，至少 32 字元（`openssl rand -hex 32`） |
| `FUBON_REQUEST_TIMEOUT_SEC` | 否 | 每個富邦請求的逾時秒數，預設 `10` |
| `FUBON_RATE_LIMIT_PER_SEC` | 否 | 每秒對富邦的請求上限，預設 `5` |

token 少於 32 字元時 sidecar 拒絕啟動。登入被富邦拒絕時**不會自動重試**（連續密碼錯誤可能鎖帳號），
請修正設定後重啟容器；登入時的網路錯誤則會退避重試。log 不會輸出帳號、密碼、API key 或 token。

## 4. 啟動

```bash
docker compose --profile taiwan up -d --build fubon-marketdata
docker compose --profile taiwan ps fubon-marketdata
```

服務位於 compose 的 `taiwan` profile，單純 `docker compose up -d` 不會啟動它。它不對主機發布埠，
只有 `quantdinger-network` 內的容器能連到 `http://fubon-marketdata:8010`。SDK 登入且行情初始化完成後，
容器 healthcheck 才會變成 healthy。

## 5. 驗證

埠沒有對外發布，請在 sidecar 容器內驗證（token 從容器環境變數讀取，不需在指令列輸入）：

```bash
docker compose --profile taiwan exec fubon-marketdata python -m fubon_sidecar.probe /health
```

預期：`{"status": "ok", "logged_in": true, "realtime_ready": true, "sdk_version": "2.2.8"}`。
接著確認實際資料：

```bash
P="docker compose --profile taiwan exec fubon-marketdata python -m fubon_sidecar.probe"
$P '/v1/stock/candles?symbol=2330&timeframe=D'
$P '/v1/stock/intraday/candles?symbol=IX0001&timeframe=1'
$P '/v1/futopt/tickers?type=FUTURE'          # 找出 TXF 近月合約代碼，例如 TXFL6
$P '/v1/futopt/intraday/candles?symbol=TXFL6&timeframe=1&session=regular'
$P '/v1/futopt/intraday/candles?symbol=TXFL6&timeframe=1&session=afterhours'
```

## 6. 每日 K 線累積

富邦的股票分 K 只保留近 5 個交易日、期貨分 K 只有當盤。因此後端的 `celery-beat` 會排程
`quantdinger.tasks.tw_kline_collect`（maintenance queue，每 `TW_KLINE_COLLECT_INTERVAL_SEC`
秒，預設 600 秒），把每個盤別的 1 分 K 寫進 PostgreSQL 的 `qd_tw_klines`：

| 市場 | 盤別（`session`） | 何時開始抓（台北時間） | 失敗重試到 |
|---|---|---|---|
| `TWStock` | `regular` | 交易日 14:00 後 | 之後第 3 個交易日 08:00 |
| `TWFutures` | `regular`（日盤） | 交易日 13:50 後 | 下一個交易日 08:30 |
| `TWFutures` | `afterhours`（夜盤） | 隔天 05:10 後 | 下一個交易日 08:30 |

- 交易日依 `exchange_calendars` 的 `XTAI` 判斷，非交易日不抓。
- 期貨依合約存（例如 `TXFH6`），抓當時的近月與次月（依 `settlementDate`），不存連續代碼。
- 累積清單：`TW_KLINE_COLLECT_SYMBOLS`（`Market:Symbol` 逗號分隔，預設
  `TWStock:IX0001,TWStock:2330.TW,TWStock:0050.TW,TWFutures:TXF,TWFutures:MXF`），再加上所有使用者
  watchlist 中的 `TWStock`／`TWFutures` 代碼。`TW_KLINE_COLLECT_ENABLED=false` 可關閉；
  `FUBON_SIDECAR_TOKEN` 為空時工作會自動略過。
- 每個（代碼、盤別、交易日）的結果寫在 `qd_tw_kline_collect_status`：`success`、`failed`
  （下一次排程重抓）或 `missed`（重試期限已過，log 會出現 `TW K-line gap` warning）。查缺口：
  `SELECT * FROM qd_tw_kline_collect_status WHERE status <> 'success' ORDER BY trade_date DESC;`
- 寫入以 `(market, symbol, session, timeframe, bar_time)` upsert，重跑冪等；同一根 K 棒以 `fubon`
  為準，之後匯入的 `taifex` 資料不會覆蓋它。

**資料從哪一天開始有：** 分 K 歷史從這個工作第一次執行的交易日開始。第一次執行時也會一併存下股票近 5
個交易日內仍可取得的資料；更早的日期要等期交所歷史匯入。策略讀取 `TWStock` 分 K／小時 K 時先讀這張表，
最近幾天再由 sidecar 補齊；回測的 data-coverage 會回報表中最早的一根。

## HTTP 介面（v1）

所有請求（含 `/health`）都必須帶 `X-Sidecar-Token: <FUBON_SIDECAR_TOKEN>`，否則回
`401 {"code": "unauthorized"}`。所有時間一律為 unix 秒（UTC）；tickers 裡的合約日期
（`endDate`、`settlementDate`）維持 `YYYY-MM-DD` 字串。

| 端點 | 對應 SDK | 說明 |
|---|---|---|
| `GET /health` | — | `{status, logged_in, realtime_ready, sdk_version}`；`status` 為 `ok`、`starting` 或 `error` |
| `GET /v1/stock/candles?symbol&timeframe&from&to` | `stock.historical.candles` | `D`/`W`/`M` 可指定 1 年內的 `from`/`to`（`YYYY-MM-DD` 或 unix 秒）；`1`/`5`/`10`/`15`/`30`/`60` 分 K 富邦只回近 5 日，帶 `from`/`to` 會回 400 |
| `GET /v1/stock/intraday/candles?symbol&timeframe` | `stock.intraday.candles` | 當日分 K（含指數，例如 `IX0001`） |
| `GET /v1/stock/quote?symbol` | `stock.intraday.quote` | 最新價、漲跌、開高低、昨收、名稱 |
| `GET /v1/stock/tickers?type&exchange` | `stock.intraday.tickers` | `type` `EQUITY`/`INDEX`/`WARRANT`/`ODDLOT`；`exchange` `TWSE`/`TPEx` |
| `GET /v1/futopt/intraday/candles?symbol&timeframe&session` | `futopt.intraday.candles` | `session` `regular`（日盤，預設）或 `afterhours`（夜盤） |
| `GET /v1/futopt/quote?symbol&session` | `futopt.intraday.quote` | 最新價、`previousClose` 等 |
| `GET /v1/futopt/tickers?type` | `futopt.intraday.tickers` | `type` `FUTURE`/`OPTION`；含 `symbol`、`name`、`endDate`、`settlementDate` |

K 線回傳 `{symbol, timeframe, session?, candles: [{time, open, high, low, close, volume}]}`，依時間升冪。
富邦 K 線的時間在 `date` 欄位（ISO 字串，例如 `2026-07-06T08:45:00.000+08:00`；日 K 為
`YYYY-MM-DD`，視為台北時間 00:00），不是文件寫的 `time`。

報價回傳 `{symbol, name, lastPrice, change, changePercent, open, high, low, previousClose,
referencePrice, volume, time}`（期貨／選擇權另含 `session`）。

錯誤格式為 `{code, message?}`：

| 狀態碼 | `code` | 情況 |
|---|---|---|
| 400 | `invalidRequest` | 參數缺漏或不合法 |
| 401 | `unauthorized` | 沒帶或帶錯 `X-Sidecar-Token` |
| 404 | `notFound` | 富邦查無資料 |
| 429 | `rateLimited` | 在逾時內無法滿足本地每秒上限 |
| 502 | `upstreamError` | SDK 例外或逾時（message 不含機密） |
| 503 | `notReady` | 尚未登入或行情尚未初始化 |

## 開發

```bash
cd fubon_sidecar
python -m pip install -e ".[dev]"
ruff check .
python -m pytest -q
```

測試透過 `sdk_factory` 注入假的 SDK，不會連線富邦。CI（`.github/workflows/fubon-sidecar-ci.yml`）
只在 `fubon_sidecar/**` 變動時執行，且不安裝 `fubon_neo`。
