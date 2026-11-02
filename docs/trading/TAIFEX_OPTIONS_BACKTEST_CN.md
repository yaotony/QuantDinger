# 期交所選擇權歷史資料與離線選擇權回測器

[English](TAIFEX_OPTIONS_BACKTEST.md)

> **僅供研究。結果不得當成可實盤的績效。**
> 回測器只用期交所日資料，沒有買賣價，保證金也是近似值。每份結果都帶有
> `not_live_performance: true`。

Strategy API V2 沒有選擇權商品類型。送進 `submit_backtest`、回測服務或模擬盤／實盤部署的
選擇權合約，仍會回 `strategyV2.optionsBacktestUnsupported`（#59）。以下工具是獨立的：
一個期交所選擇權每日行情的匯入器，以及一個只做 TXO 月選的離線回測器，兩者都不接
Strategy API V2、模擬盤或實盤。

## 1. 選擇權每日行情

### 資料表

`qd_tw_option_daily`（每個交易日、商品、到期月份、履約價、買賣權、交易時段一列）：

| 欄位 | 說明 |
| --- | --- |
| `trade_date` | 期交所公布的「交易日期」原值。日期 D 的「盤後」列是 D 日 15:00 開盤的夜盤。 |
| `product` | 期交所商品代號，例如 `TXO` |
| `expiry` | 到期月份 `202608`，或週別如 `202608W1` |
| `strike` | 履約價 |
| `option_right` | `call`（買權）或 `put`（賣權） |
| `session` | `regular`（一般）或 `afterhours`（盤後） |
| `symbol` | 月選用 #59 的代碼（`TXO23000H6`）；週選留空 |
| `open`、`high`、`low`、`close` | 價格；當天無成交（`-`）時為 `NULL` |
| `volume` | 成交量（無成交為 0） |
| `settlement_price` | 每日結算價；期交所未公布時為 `NULL` |
| `open_interest` | 未沖銷契約數 |
| `source` | `taifex` |

`qd_tw_option_settlement` 存每個商品、每個契約月份的最後結算價（`settlement_date`、
`final_settlement_price`）。TXO 是現金結算的歐式選擇權，沒有最後結算價就無法處理到期部位。

兩張表都寫在 `migrations/init.sql` 與已註冊的 migration `20261103_tw_option_daily.sql`
（`CREATE TABLE IF NOT EXISTS`），屬於所有租戶共用的行情資料。每次寫入都是依唯一鍵 upsert，
重跑不會產生重複列，修正過的值會取代舊值。

### 匯入

在後端環境內執行（與 API 相同的資料庫設定）：

```bash
cd backend_api_python
python scripts/import_taifex_options_history.py --from 2020-01-01 --to 2026-10-31 --products TXO
```

- 每日選擇權行情從 `https://www.taifex.com.tw/cht/3/optDataDown` 以一個月為單位下載，
  請求間隔 1 秒（`--delay`）。
- 同一期間的最後結算價從 `https://www.taifex.com.tw/cht/5/fSPriceDown` 下載
  （`--skip-settlements` 可略過）。
- 失敗的月份會列在輸出的 `failed`，結束碼為 1；重跑同一指令（或只跑該區間）即可補上。
- 也可以匯入手動下載的檔案：`--daily-file <csv/zip ...>`、`--settlement-file <csv ...>`。
- 檔案可以是 Big5（cp950）或 UTF-8（有無 BOM 皆可），也可以是 zip。
- 欄位依表頭名稱比對（`交易日期`、`契約`、`到期月份`、`履約價`、`買賣權`、`開盤價`、
  `最高價`、`最低價`、`收盤價`、`成交量`、`結算價`、`未沖銷契約…`，`交易時段` 可有可無），
  不依欄位位置。無法解析的列（買賣權不明、日期錯誤、價差月份、缺履約價、欄位不足）計入
  `invalid_rows` 並略過。
- 最後結算價解析器接受長表（`最後結算日, 契約, 契約月份, 最後結算價`），或每個商品一欄、
  表頭寫出商品名稱的寬表（`臺指選擇權(TXO)`）。缺值（`-`）絕不會被存成 0。

**資料起點。** 參考匯入從 2020-01-01 開始。匯入器接受任何區間；期交所每日選擇權行情最早能下載到
哪一天，需在期交所網站確認。下載網址與表單欄位由 owner 確認，並抽查匯入的交易日與最後結算價
是否與期交所網站一致。

## 2. 離線回測器

套件：`backend_api_python/app/services/options_backtest/`。CLI：

```bash
cd backend_api_python
python scripts/run_options_backtest.py strategy.json --from 2024-01-01 --to 2025-12-31 \
    --cost-multiplier 2 --sample out --output result.json
```

JSON 結果寫到 `--output`（或 stdout），簡短摘要寫到 stderr。缺最後結算價或每日結算價時，
回測停止並以結束碼 1 回報。

### 第一版範圍

- 商品：只有 TXO 月選（週選忽略）。
- 結構：`long_call`、`long_put`、`short_call`、`short_put`、`bull_call_spread`、
  `bear_call_spread`、`bull_put_spread`、`bear_put_spread`、`iron_condor`。
  各腳依履約價由低到高列出（鐵禿鷹為買賣權、賣賣權、賣買權、買買權）。
- 每個部位依進場權利金記錄到期最大虧損（不含成本）；淨賣出買權的部位記為無上限
  （`max_loss: null`、`max_loss_unlimited: true`）。

### 策略檔

明確列出交易：

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

每月規則：

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

- 每一腳給絕對 `strike`，或給 `strike_offset`（相對進場日以買賣權平價推得的標的價，單位為指數點），
  再取最接近的掛牌履約價。標的價 = 在 `|C − P|` 最小的履約價上計算 `K + C − P`，使用結算價。
- 規則進場鎖定剩餘日曆天數至少 `min_days_to_expiry` 的最近到期月份，在剩餘天數不超過
  `entry_days_before_expiry` 時進場；每個到期月份一個部位，被拒絕的進場在下一個交易日重試。
- 明確列出的交易只在 `entry_date` 當天或之後的第一個交易日嘗試一次。
- `exit_date`／`exit_days_before_expiry`（0 = 持有到到期）提前平倉；平倉受阻時每個交易日重試，
  直到到期日。

### 每日處理順序

1. 排定的平倉。
2. 新進場：先過流動性門檻，再檢查保證金。
3. 到期日已到的部位以現金結算。
4. 所有未平倉部位以當日結算價評價，記錄淨值曲線與保證金占用。

不會略過任何部位：無法評價或結算的部位會讓回測以明確錯誤停止；期末仍未平倉的部位列在
`open_positions`，不計入交易筆數。

### 成交與流動性

- 參考價：當日收盤價（`fill_price: "close"`）或結算價（`"settlement"`），買進加、賣出減
  `slippage_ticks` 個 TXO 跳動點（跳動點：10 點以下 0.1、50 點以下 0.5、500 點以下 1、
  1,000 點以下 5、其餘 10）。
- 日資料沒有買賣價。成交量低於 `min_volume` 或未沖銷契約數低於 `min_open_interest` 的履約價一律
  不得成交（進場被拒，或平倉受阻並重試）。沒有參考價或結算價的履約價也不得成交。

### 成本

| 參數 | 預設 | 依據 | 狀態 |
| --- | --- | --- | --- |
| `multiplier` | 50 | TXO 每點新台幣 50 元 | 期交所契約規格 |
| `tax_rate` | 0.001 | 權利金期交稅 0.1%，買賣各計 | 期貨交易稅條例；待 owner 確認 |
| `settlement_tax_rate` | 0.001 | 到期現金結算金額的稅（價內的買方與賣方腳都計） | 假設；待 owner 確認 |
| `fee_per_contract` | 25 | 券商手續費（含交易所／結算費），每口每邊 | 依券商；由 owner 設定 |
| `settlement_fee_per_contract` | 25 | 到期價內結算每口手續費 | 依券商；由 owner 設定 |
| `slippage_ticks` | 1 | 研究假設 | — |
| `cost_multiplier` | 1 | 手續費、稅與滑價同乘（2 = 雙倍成本） | 研究規則 |

稅額不像券商一樣四捨五入到整數元（每筆誤差小於 1 元）。

### 到期

到期日（期交所最後交易日，取自 #59 的日曆）時，各腳以最後結算價計算內含價值並現金結算；
價外腳歸零，不收結算成本。價差的兩腳同日結算。缺最後結算價時拋出 `SettlementPriceMissing`，
絕不當成 0。

### 保證金

| 結構 | 每單位保證金 |
| --- | --- |
| 買進單腿、借方價差 | 0（權利金已全額支付） |
| 賣出單腿 | 權利金市值 + max(A 值 − 價外值, B 值) |
| 貸方垂直價差 | 履約價差 × 50 |
| 鐵禿鷹 | 兩組價差中較寬者 × 50 |

- `margin_a`／`margin_b` 預設為新台幣 85,000／43,000。期交所會調整 A、B 值，回測整段只用一組值，
  請依回測期間設定；目前數值須由 owner 對照期交所公告確認。
- 價外值用買賣權平價推得的標的價，不是加權指數收盤；推不出標的價時價外值當 0（保證金偏高）。
- 保證金 + 借方權利金 + 進場成本 超過「淨值 − 已占用保證金」時拒絕開倉；收到的權利金不計入可用額度。
- 保證金每日以結算價重算；超過淨值時記錄 `margin_shortfall` 事件，不會強制平倉。

### 輸出

`metrics` 與扁平的 `ledger` 至少包含：`total_return`、`max_drawdown`（相對歷史高點的比例，
起點為初始資金）、`trades`（已平倉部位數）、`win_rate`、`total_cost`、`max_single_loss`
（已平倉交易中最差的損益，沒有虧損時為 0）、`peak_margin`、`final_equity`、
`open_positions_at_end`、`cost_multiplier`、`sample` 與 `not_live_performance`。
結果另有 `trades`、`open_positions`、`equity_curve`（每日淨值、現金、保證金）與 `events`
（進場、附原因的拒絕、受阻平倉、到期、保證金不足）。

### 研究規則

樣本內與樣本外分開跑（`--sample in`／`--sample out`），並以 `--cost-multiplier 2` 再跑一次。
把每次的 `ledger` 物件記進研究 ledger。

## 3. 已知誤差與限制

- 只有日資料：沒有買賣價、沒有盤中路徑。以收盤或結算價加滑價成交，可能與實際可成交價差很多，
  深價外履約價尤其明顯。
- 保證金是近似值（固定 A／B 值、平價推得的標的價、只認列上述結構的組合、沒有盤中追繳）。
- 稅與手續費依設定費率計算；實際券商收費與進位方式不同。
- 標的價估計忽略利息與股利（距到期最多約一個月）。
- 週選、個股選擇權、盤中與逐筆回測不在範圍內。
