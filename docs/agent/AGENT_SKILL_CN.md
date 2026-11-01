# QuantDinger Agent Skill

[English](AGENT_SKILL.md)

QuantDinger Strategy Lab skill 教 Claude Code、Codex、OpenClaw 等會讀 `SKILL.md` 的 Agent：QuantDinger MCP 工具該依什麼順序使用、安全界線在哪裡。使用者只要用一句話描述想法，例如「幫我設計 BTC 15 分鐘的 ORB 策略並回測」，Agent 就會：

1. 用 `check_environment()` 確認環境；
2. 用 `get_data_coverage()` 確認資料涵蓋範圍，選出可行的樣本內與樣本外區間；
3. 讀撰寫規範與範本，用 `compile_strategy_code()` 編譯驗證，再用 `save_strategy_source()` 存成 strategy source；
4. 用 `submit_backtest()` 送出回測，用 `wait_for_job()` 等待完成；
5. 診斷結果：成本佔比、勝率與賺賠比能否打平、回撤、與基準比較；
6. 每輪只改一個假設，並記錄迭代紀錄。

skill 本體在 [`skills/quantdinger-strategy-lab/SKILL.md`](skills/quantdinger-strategy-lab/SKILL.md)（英文，供 Agent 讀取）。英文完整範例對話在 [`references/example-session.md`](skills/quantdinger-strategy-lab/references/example-session.md)，中文版見本文最後的[範例對話](#範例對話btc-15-分鐘-orb)。

## skill 規定的事項

- **安全規則**：只用研究用 token（R/W/B、paper-only，不給 T）；不下單、不變更執行中的策略；部署一律先用 `signal` 模式並維持停止狀態；不代替使用者接受條款，也不輸入密碼、金鑰或 token。
- **防過度擬合規則**：不為了達成報酬目標而掃參數；調參數時要在沒看過的樣本外區間（或 walk-forward 區間）驗證；回報結果時一併說明樣本數量、測試期間的市場狀況與成本假設，並附上成本加倍的結果。

## 前置條件

1. 執行中的 QuantDinger 後端，以及由使用者本人簽發的回測專用 Agent Token（scope 為 `R`、`W`、`B`，`paper_only: true`）。做法見[策略迭代指南的步驟 0](AGENT_STRATEGY_ITERATION_CN.md)。
2. 已安裝 `quantdinger-mcp` 並接上 Agent，做法見 [MCP 設定](MCP_SETUP_CN.md)。沒有 MCP 時，Agent 可以依 skill 裡的 Gateway 對照表直接用 HTTP 呼叫 `/api/agent/v1`。

token 放在用戶端的環境變數或密鑰管理中，不可寫進 skill 檔案、提示詞或 repo。

## 安裝

skill 是一個資料夾：請複製整個 `docs/agent/skills/quantdinger-strategy-lab` 目錄（含 `references/`）。以下指令在 repo 根目錄執行，`qd_agent_xxx` 是 token 的佔位字。

### Claude Code

安裝給目前使用者（所有專案）或單一專案：

```bash
mkdir -p ~/.claude/skills
cp -R docs/agent/skills/quantdinger-strategy-lab ~/.claude/skills/
# 或只安裝到單一專案：
mkdir -p .claude/skills
cp -R docs/agent/skills/quantdinger-strategy-lab .claude/skills/
```

註冊 MCP server：

```bash
claude mcp add quantdinger \
  -e QUANTDINGER_BASE_URL=http://localhost:8888 \
  -e QUANTDINGER_AGENT_TOKEN=qd_agent_xxx \
  -- quantdinger-mcp
```

開新的 session。需求符合 skill 描述時 Claude Code 會自動載入，也可以輸入 `/quantdinger-strategy-lab` 手動呼叫。

### Codex

把 skill 複製到 Codex 的 skills 目錄：

```bash
mkdir -p ~/.codex/skills
cp -R docs/agent/skills/quantdinger-strategy-lab ~/.codex/skills/
```

在 `~/.codex/config.toml` 加入 MCP server：

```toml
[mcp_servers.quantdinger]
command = "quantdinger-mcp"
env = { QUANTDINGER_BASE_URL = "http://localhost:8888", QUANTDINGER_AGENT_TOKEN = "qd_agent_xxx" }
```

重新啟動 Codex，讓它載入 skill 與 server。若你使用的 Codex 版本從其他目錄讀取 skill，把同一個資料夾複製過去即可。

### OpenClaw

把 skill 複製到共用的 skills 目錄，或單一 workspace 的 `skills/` 資料夾：

```bash
mkdir -p ~/.openclaw/skills
cp -R docs/agent/skills/quantdinger-strategy-lab ~/.openclaw/skills/
```

依 OpenClaw 的 MCP 設定方式，用上面相同的指令與環境變數接上 `quantdinger-mcp`；或讓 Agent 依 skill 的 Gateway 對照表，用密鑰管理中的 `QUANTDINGER_AGENT_TOKEN` 呼叫 HTTP。開新的 session 讓 skill 生效。

## 讓 skill 與契約保持一致

skill 只能引用已存在的 MCP 工具與 Gateway 操作。`scripts/check_agent_skill.py` 會檢查：skill 與這兩份說明中的每個 MCP 工具呼叫都是已註冊的工具、參數名稱與實際簽名一致；每個 `METHOD /path` 都存在於 [agent-openapi.json](agent-openapi.json)；frontmatter 格式正確。CI 透過 `backend_api_python/tests/test_agent_skill_contract.py` 執行它；本機可在 repo 根目錄執行：

```bash
python3 scripts/check_agent_skill.py
```

MCP 工具改名或參數變更時，要在同一個變更中更新 skill。專用的 walk-forward 或樣本外驗證工具上線後，把 skill 的防過度擬合步驟改用該工具。

## 範例對話：BTC 15 分鐘 ORB

以下是 skill 從一句話需求到診斷結果的完整流程。工具回應只節錄 Agent 用到的欄位，數字僅為示意；實際執行時以伺服器回傳為準。策略原始碼見[英文範例](skills/quantdinger-strategy-lab/references/example-session.md)。

**使用者：** 幫我設計 BTC 15 分鐘的 ORB 策略並回測。

**Agent：** 先確認環境。

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

**Agent：** token 只有研究用 scope（R、W、B）、是 paper-only，兩個回測名額都空著。我要測的規格如下，預設值不對請告訴我：

| 項目 | 設定 |
| --- | --- |
| 標的 | `Crypto:BTC/USDT@spot`（Binance 資料） |
| 週期 | 15m |
| 開盤區間 | UTC 00:00 後的前 4 根 K 棒（1 小時） |
| 進場 | 15m K 棒收盤突破區間高點時做多，每天最多進場一次 |
| 出場 | 1% 停損，否則在下一個 UTC 日開始時平倉 |
| 方向／部位 | 只做多，權益的 50%，不開槓桿 |
| 成本 | 後端預設值（單邊手續費 0.1%、滑價 0.05%） |

**使用者：** 可以。

**Agent：** 確認資料涵蓋範圍。我打算用 2025-01-01 至 2025-06-30 開發，2025-07-01 至 2025-09-30 保留不看，留作樣本外驗證。

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

**Agent：** 兩段區間都可行（272 天加 2 天暖機，上限 365 天）。接著讀撰寫規範與範本。

```text
get_strategy_authoring_contract()
list_strategy_templates(limit=20)
```

**Agent：** 以突破範本為基礎寫好原始碼（`range_bars`、`stop_pct`、`target_pct` 三個 `# @param`，暖機 96 根 K 棒，只做多），編譯驗證：

```text
compile_strategy_code(code="<策略原始碼>")
```

```json
{"manifest": {"universe": ["Crypto:BTC/USDT@spot"], "frequencies": ["15m"],
              "drivingFrequency": "15m", "warmupBars": 96,
              "directionMode": "long_only", "leverageAllowed": false}}
```

**Agent：** 編譯通過，manifest 與規格一致（暖機 96 根，與剛才檢查的相同）。存檔並回測：

```text
save_strategy_source(name="btc-orb-15m", code="<策略原始碼>",
                     description="ORB v1: first-hour breakout, 1% stop",
                     idempotency_key="lab-orb-v1-save")
submit_backtest(code="<策略原始碼>", start_date="2025-01-01", end_date="2025-06-30",
                initial_capital=10000, params={}, idempotency_key="lab-orb-v1-bt")
wait_for_job(job_id="job-1")
```

```json
{
  "status": "succeeded",
  "result": {
    "runId": 301, "resultStatus": "completed_trades",
    "totalReturn": -6.8, "benchmarkTotalReturn": 14.9, "maxDrawdown": 9.4,
    "sharpeRatio": -0.71, "totalTrades": 112, "winRate": 41.1,
    "profitLossRatio": 1.31, "profitFactor": 0.91, "totalCommission": 1120.0,
    "executionAssumptions": {"commission": 0.001, "slippage": 0.0005, "fundingMode": "not_modeled"}
  }
}
```

**Agent：** 第 1 輪虧 6.8%，同期持有 BTC 賺 14.9%。診斷：

- **損益兩平：** 賺賠比 1.31 時，勝率要 43.3%（`1 / (1 + 1.31)`）才打平；實際勝率 41.1%，本身就略虧。
- **成本吃掉獲利：** 112 次來回、每次約 5,000 USDT 名目金額，手續費約 1,120 USDT、滑價約 560 USDT，合計約佔起始資金 16.8%。扣成本前約 +10%，扣完變成 -6.8%。
- **原因：** 多數進場發生在開盤區間很窄的日子，1% 停損與 0.3% 來回成本相對於一般波動太大。

第 2 輪假設：開盤區間寬度小於價格 0.4% 的日子不交易，因為 0.3% 的來回成本需要更大的波動才划算。這只改一件事：新增一個過濾條件，數值來自成本試算，不是掃參數得來的。

```text
save_strategy_source(source_id=12, name="btc-orb-15m", code="<加上 min_range_pct 過濾的原始碼>",
                     description="ORB v2: skip opening ranges narrower than 0.4%",
                     idempotency_key="lab-orb-v2-save")
submit_backtest(code="<v2 原始碼>", start_date="2025-01-01", end_date="2025-06-30",
                initial_capital=10000, params={}, idempotency_key="lab-orb-v2-bt")
wait_for_job(job_id="job-2")
```

```json
{"status": "succeeded",
 "result": {"runId": 302, "totalReturn": 3.9, "maxDrawdown": 4.1, "sharpeRatio": 0.9,
            "totalTrades": 46, "winRate": 47.8, "profitLossRatio": 1.42,
            "profitFactor": 1.3, "totalCommission": 460.0}}
```

**Agent：** v2 勝率 47.8%，超過兩平所需的 41.3%，交易次數從 112 降到 46。因為策略改過，我在沒看過的樣本外區間跑一次，再用加倍成本跑一次。

```text
submit_backtest(code="<v2 原始碼>", start_date="2025-07-01", end_date="2025-09-30",
                initial_capital=10000, params={}, idempotency_key="lab-orb-v2-oos")
submit_backtest(code="<v2 原始碼>", start_date="2025-01-01", end_date="2025-06-30",
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

**Agent：** 結果報告：

| # | 變更 | 版本 | 區間 | 成本 | 交易數 | 報酬 % | 最大回撤 % | 勝率 % | runId |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 基準 ORB | 1 | 2025-01-01 至 2025-06-30 | 預設 | 112 | -6.8 | 9.4 | 41.1 | 301 |
| 2 | 開盤區間 < 0.4% 不交易 | 2 | 2025-01-01 至 2025-06-30 | 預設 | 46 | 3.9 | 4.1 | 47.8 | 302 |
| 3 | v2 樣本外 | 2 | 2025-07-01 至 2025-09-30 | 預設 | 19 | 0.6 | 3.2 | 42.1 | 303 |
| 4 | v2 成本加倍 | 2 | 2025-01-01 至 2025-06-30 | 2 倍 | 46 | -0.7 | 無 | 無 | 304 |

- **樣本數量：** 樣本內 46 筆、樣本外 19 筆，共測了 4 個版本。19 筆太少，分不出是策略有效還是運氣。
- **市場狀況：** 樣本內半年整體上漲（BTC +14.9%），春季有一段急跌；樣本外這一季漲得較平穩（+8.1%）。兩段都沒有長期空頭或盤整，結果無法推論到那些行情。
- **成本假設：** 單邊手續費 0.1%、滑價 0.05%，未計入加密貨幣資金費率。成本加倍後 v2 轉為虧損，優勢很薄。
- **診斷：** 開盤區間過濾在樣本內解決了成本問題，但樣本外勝率回到 42.1%，只比 41.3% 的兩平點高一點；兩個版本都輸給單純持有。
- **結論：** v2 還不夠穩健，不建議部署。source 12（第 2 版）已保存，可繼續研究。

接下來可以試的假設（每輪一個）：要求突破 K 棒的成交量高於開盤區間平均，或改在包含盤整行情的期間測試。要繼續其中一個嗎？

**使用者：** 先不用。之後如果變好，你能幫我部署嗎？

**Agent：** 當某個版本通過樣本外與成本檢查後，我可以用 `create_strategy(execution_mode="signal", ...)` 以 `signal` 模式建立部署。它建立時是停止狀態，只產生訊號。啟動、連接交易所帳戶以及任何轉為實盤的動作，都由你在網頁介面決定。
