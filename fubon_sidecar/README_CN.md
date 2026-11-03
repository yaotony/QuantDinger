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
- 預設不累積選擇權（合約太多）：watchlist 裡的選擇權合約與 `TWFutures:TXO` 這類選擇權商品都會略過。
  要保留少數合約時，把合約列在 `TW_KLINE_COLLECT_SYMBOLS`，例如
  `TWFutures:TXO23000H6,TWFutures:TXO23000T6`。
- 每個（代碼、盤別、交易日）的結果寫在 `qd_tw_kline_collect_status`：`success`、`failed`
  （下一次排程重抓）或 `missed`（重試期限已過，log 會出現 `TW K-line gap` warning）。查缺口：
  `SELECT * FROM qd_tw_kline_collect_status WHERE status <> 'success' ORDER BY trade_date DESC;`
- 寫入以 `(market, symbol, session, timeframe, bar_time)` upsert，重跑冪等；同一根 K 棒以 `fubon`
  為準，之後匯入的 `taifex` 資料不會覆蓋它。

**資料從哪一天開始有：** 分 K 歷史從這個工作第一次執行的交易日開始。第一次執行時也會一併存下股票近 5
個交易日內仍可取得的資料；更早的日期要等期交所歷史匯入。策略讀取 `TWStock` 分 K／小時 K 時先讀這張表，
最近幾天再由 sidecar 補齊；回測的 data-coverage 會回報表中最早的一根。

## 7. 匯入期交所期貨歷史

富邦沒有期貨歷史 K 線，過去的 `TWFutures` K 棒改由期交所公開資料補，寫進同一張 `qd_tw_klines`，
`source=taifex`。在 `backend_api_python/` 下、用後端的資料庫設定執行：

```bash
# 日 K（期交所每日行情下載，每個商品每個月一次請求，間隔 1 秒）
python scripts/import_taifex_history.py --daily --from 2020-01-01 --to 2026-10-31 --products TX,MTX,TMF
# 已從期交所下載的逐筆檔轉 1 分 K（Daily_YYYY_MM_DD 的 .csv/.zip，可給檔案或目錄）
python scripts/import_taifex_history.py --ticks ~/Downloads/taifex/
# 直接下載並匯入指定交易日的逐筆檔（期交所只保留近 30 日）
python scripts/import_taifex_history.py --tick-days 2026-10-29,2026-10-30
```

- **商品代號：** 期交所的 `TX`、`MTX`、`TMF`、`TE`、`TF`、`ZEF`、`ZFF` 分別存成富邦代碼 `TXF`、`MXF`、
  `TMF`、`EXF`、`FXF`、`ZEF`、`ZFF`（`--products` 兩種寫法都接受）。依月份合約存（`TXFH6`），
  週別合約（`202605W4`）與價差不匯入。
- **每日行情 → `timeframe=1D`：** 每個合約、盤別、交易日一根，時間標在台北時間 00:00。`一般` 是日盤；
  日期 D 的 `盤後` 是 D 當天 15:00 開盤的夜盤，歸屬下一個交易日。沒資料的月份列在 `empty`，失敗的月份
  列在 `failed`（指令回傳 1），重跑同一個指令即可補。
- **逐筆檔 → `timeframe=1m`：** 依分鐘彙總；成交數量(B+S) 是買賣雙邊合計，口數為數量除以 2。盤別依該分鐘
  判斷（日盤 08:30-14:00、夜盤 14:50-05:10）。檔案可以是 Big5 或 UTF-8。
- **可重跑：** 所有寫入都是 upsert，已有 `source=fubon` 的 K 棒不會被覆蓋。
- **分工：** 期交所 OpenAPI 只提供最新一個交易日，由下面兩個 Celery Beat 排程每天自動收集；
  這支腳本保留網頁下載，用來手動匯入 OpenAPI 範圍以外的歷史（多年日 K、較早日子的逐筆檔）。
- **每日日 K：** `quantdinger.tasks.taifex_daily_collect`（maintenance queue，每
  `TAIFEX_DAILY_COLLECT_INTERVAL_SEC` 秒，預設 1800 秒）讀期交所 OpenAPI `DailyMarketReportFut`，
  把 `TAIFEX_DAILY_COLLECT_PRODUCTS`（預設 `TX,MTX`）存成 `source=taifex` 的 1D K 棒。報表約在交易日
  隔天清晨 06:35 公布，台北時間 `TAIFEX_DAILY_COLLECT_NOT_BEFORE`（預設 `07:30`）之前不執行；最近 5 個
  交易日都收齊後每次只查一次狀態表。每天的結果以 symbol `TAIFEX_DAILY` 記在
  `qd_tw_kline_collect_status`；OpenAPI 已換成下一天、沒收到的日子記為 `missed`，要用 `--daily` 補。
  有待收日子的那幾次執行完會比對 `qd_tw_klines` 中 `source=taifex` 日盤 1D 的最新交易日與 XTAI 日曆
  應有的最新交易日，落後時記 `TAIFEX daily bars are stale` warning。
  `TAIFEX_DAILY_COLLECT_ENABLED=false` 可關閉；`FUBON_SIDECAR_TOKEN` 為空時自動略過。
- **每日補缺口：** `quantdinger.tasks.taifex_tick_backfill`（maintenance queue，每
  `TAIFEX_TICK_BACKFILL_INTERVAL_SEC` 秒，預設 3600 秒）匯入今天以前最近 5 個交易日的逐筆成交
  （商品依 `TAIFEX_TICK_BACKFILL_PRODUCTS`，預設 `TX,MTX`），補上富邦累積漏掉的分鐘。期交所 OpenAPI
  （`TimeAndSalesData`，先探測）目前提供的那個交易日走 OpenAPI 串流；其他日子改用網頁
  `Daily_YYYY_MM_DD.zip`（約保留 30 天）。超過 30 天仍未補到的日子記為 `missed` 並記 warning，
  可用 `--ticks` 匯入自行保存的逐筆檔。每天的結果以 symbol `TAIFEX_TICKS` 記在
  `qd_tw_kline_collect_status`，狀態文字開頭為 `source=openapi` 或 `source=web_zip`；失敗（例如
  還沒公布）下一次排程重抓。
  `TAIFEX_TICK_BACKFILL_ENABLED=false` 可關閉；`FUBON_SIDECAR_TOKEN` 為空時自動略過。

**匯入後可回測的範圍：**

| 週期 | 資料來源 | 最早可到 |
|---|---|---|
| `1D`、`1W` | 有 1 分 K 的日子用 1 分 K，其餘用期交所日 K（匯入或每日收集） | `--daily --from` 的日期（期交所每日行情可回溯多年） |
| `1m` … `4H` | 只用已存的 1 分 K（富邦累積 + 期交所逐筆檔） | 第一次匯入逐筆檔往前約 30 個交易日，之後連續 |

連續代碼（`TXF`）每天從已存的合約中取當時的近月，所以要匯入曾經是近月的每個月份（每日行情本身就包含所有
掛牌月份）。`TWFutures:TXF` 的 data-coverage 在 `1D`／`1W` 回報第一根日 K，較短週期回報第一根 1 分 K。
單次 `1D`／`1W` 回測區間仍最長 2 年。

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
| `GET /v1/stock/tickers?type&exchange&market` | `stock.intraday.tickers` | `type` `EQUITY`/`INDEX`/`WARRANT`/`ODDLOT`；`exchange` `TWSE`/`TPEx`；選填市場別 `market` `TSE`/`OTC`/`ESB`/`TIB`/`PSB`；每列為 `{symbol, name}` |
| `GET /v1/futopt/intraday/candles?symbol&timeframe&session` | `futopt.intraday.candles` | `session` `regular`（日盤，預設）或 `afterhours`（夜盤） |
| `GET /v1/futopt/quote?symbol&session` | `futopt.intraday.quote` | 最新價、`previousClose` 等 |
| `GET /v1/futopt/tickers?type` | `futopt.intraday.tickers` | `type` `FUTURE`/`OPTION`；含 `symbol`、`name`、`endDate`、`settlementDate` |

股票代碼清單（`/v1/stock/tickers`）：富邦文件列出的篩選參數有 `type`、`exchange`、`market`、
`industry`、`isNormal`、`isAttention`、`isDisposition`、`isHalted`；每列只有 `symbol` 與 `name`，
所以市場別要靠 `market` 篩選。sidecar 會轉送 `market`，並在回應中帶回（`{type, exchange, market, tickers}`）。
市場別代碼：`TSE` 上市、`OTC` 上櫃、`TIB` 臺灣創新板，興櫃為 `ESB`（興櫃一般板）與 `PSB`（興櫃戰略新板）。
全額交割股沒有對應的篩選參數或欄位；`isNormal` 只區分注意、處置與暫停交易股票。

後端 TWStock 代碼表只查 `TWSE`+`TSE`、`TWSE`+`TIB`、`TPEx`+`OTC` 的 `EQUITY`，興櫃代碼不會進入代碼表。
清單完整時（兩個交易所都有股票，且至少保留目前啟用列的 70%），本次沒出現的 TWStock 代碼會標為停用；
sidecar 失敗或回傳不完整時不會停用任何代碼。

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
