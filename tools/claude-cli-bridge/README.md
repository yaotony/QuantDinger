# Claude CLI Bridge

在本機提供 OpenAI 相容的 API，內部呼叫 Claude Code CLI（`claude -p`）。QuantDinger 的「Custom API」LLM 提供者可以透過它，使用這台電腦上已登入的 Claude。

> [!WARNING]
> **使用前請先了解兩項風險。**
>
> - **使用條款**：若 Claude CLI 是以 Pro 或 Max 訂閱登入，把它當作其他應用程式的模型後端，可能不符合 Anthropic 對訂閱方案的使用規範。是否採用由你自行判斷，實際規範以 Anthropic 官方條款為準。
> - **不要用在實盤的 AI 進場過濾**：每次請求都會啟動一個 CLI 行程，回應比直接呼叫 API 慢好幾秒，也可能因額度用盡或登入失效而失敗。這些延遲與失敗會直接影響下單。請只用在 AI 對話、研究報告、策略生成等不影響即時交易的功能。

## 運作方式

```
QuantDinger 容器 ──HTTP──▶ host.docker.internal:18790 ──▶ claude -p ──▶ Claude
   （Custom API）            （本服務，只監聽 127.0.0.1）      （Max 訂閱登入）
```

- 每個請求執行一次 `claude -p`，並關閉所有工具、MCP 伺服器、hooks、設定檔與斜線指令，讓 Claude 只當純文字模型使用。
- CLI 在一個空的工作目錄執行，不會讀到任何專案的 `CLAUDE.md`。
- 服務只監聽 `127.0.0.1`。Docker Desktop 的容器仍可透過 `host.docker.internal` 連進來，區域網路上的其他電腦則連不到。

### 支援的功能

| 功能 | 說明 |
|---|---|
| `POST /v1/chat/completions` | 一般對話，支援多輪訊息與 system prompt |
| 串流 `stream: true` | 以 SSE 逐段回傳，結尾送出 `[DONE]` |
| JSON 模式 `response_format` | 支援 `json_object` 與 `json_schema`。回傳前會驗證 JSON，必要時剝除程式碼框，失敗時重試一次 |
| `GET /v1/models` | 列出可用的模型別名 |
| `GET /health` | 健康檢查，不需要 API Key |

### 不支援的功能

- 圖片等非文字輸入，會回傳 400 錯誤。
- 工具呼叫，也就是 `tools` 與 `function_call`。
- `temperature`、`max_tokens` 等取樣參數。這些參數會被接受，但不會傳給 CLI。

## 安裝

### 事前準備

- macOS，並已安裝 Python 3.11 以上版本。
- 已安裝 Claude Code，並在終端機執行過 `claude` 完成登入。

### 安裝成常駐服務

```bash
cd tools/claude-cli-bridge
./scripts/launchd.sh install
```

安裝腳本會依序完成以下工作：

1. 在 `~/.local/share/claude-cli-bridge/venv` 建立獨立的 Python 環境並安裝本服務。安裝的是複本，所以服務不依賴這個 repo 的位置，repo 放在外接硬碟也沒問題。
2. 建立 `~/.config/claude-cli-bridge/config.toml`，自動產生隨機 API Key，並填入 `claude` 的路徑。檔案權限為 600。若設定檔已存在，會保留原內容。
3. 建立 launchd 服務 `com.quantdinger.claude-cli-bridge`，登入後自動啟動，異常結束時自動重啟。
4. 等待健康檢查通過，然後印出 QuantDinger 需要填寫的設定。

### 管理指令

```bash
./scripts/launchd.sh status     # 查看服務狀態與健康檢查
./scripts/launchd.sh restart    # 修改設定檔後重新啟動
./scripts/launchd.sh install    # 更新程式碼後重新安裝
./scripts/launchd.sh uninstall  # 移除服務，保留設定檔
```

日誌位置是 `~/Library/Logs/claude-cli-bridge/bridge.log`。每個請求記錄一行，內容包含模型、狀態碼與耗時，不會記錄 prompt、回覆內容或 API Key。

## 在 QuantDinger 設定

1. 查出 API Key：

   ```bash
   grep api_key ~/.config/claude-cli-bridge/config.toml
   ```

2. 開啟 QuantDinger，進入「系統設定」的「AI/LLM 配置」。
3. 依下表填寫，然後按「儲存設定」：

   | 欄位 | 值 |
   |---|---|
   | LLM 提供者 | Custom API (OpenAI-compatible) |
   | Custom API URL | `http://host.docker.internal:18790/v1` |
   | Custom API Key | 第 1 步查到的 API Key |
   | Custom Model | `sonnet`，也可以填 `opus` 或 `haiku` |

模型名稱也接受 `anthropic/sonnet` 這類帶前綴的寫法。只有設定檔 `models` 清單中列出的名稱會被接受；若要指定 `claude-sonnet-5-5` 這類完整模型 ID，請先把它加進清單。

## 設定檔

完整欄位與說明見 [config.example.toml](config.example.toml)。每個欄位都可以用環境變數 `CLAUDE_BRIDGE_<欄位名稱大寫>` 覆寫，例如 `CLAUDE_BRIDGE_PORT=18791`。

常用的調整項目：

| 欄位 | 預設值 | 說明 |
|---|---|---|
| `port` | `18790` | 監聽埠號。修改後 QuantDinger 的 URL 也要一起改 |
| `models` | `sonnet`、`opus`、`haiku` | 允許使用的模型名稱。只有列在這裡的名稱會傳給 CLI |
| `default_model` | `sonnet` | 請求沒有指定模型時使用的模型，必須列在 `models` 中 |
| `max_concurrency` | `2` | 同時執行的 CLI 數量上限 |
| `overflow` | `queue` | 超過上限時的處理方式。`queue` 會排隊等待，`reject` 會立即回 429 |
| `queue_timeout_seconds` | `60` | 排隊等待的時間上限 |
| `request_timeout_seconds` | `300` | 單一請求的時間上限，逾時會終止 CLI 並回 504 |
| `unknown_model` | `error` | 遇到未知模型時，`error` 回 404，`default` 改用預設模型 |

修改設定後，執行 `./scripts/launchd.sh restart` 才會生效。

## 疑難排解

| 現象 | 原因與處理 |
|---|---|
| 錯誤代碼 `cli_not_authenticated` | CLI 登入失效。在終端機執行 `claude` 重新登入，再執行 `restart` |
| 錯誤代碼 `cli_usage_limit` | 訂閱額度已用完，等額度重置後再試 |
| 錯誤代碼 `bridge_busy` | 同時請求太多。調高 `max_concurrency` 或 `queue_timeout_seconds` |
| 錯誤代碼 `cli_timeout` | 回應超過 `request_timeout_seconds`。長篇報告可以調高這個值 |
| 錯誤代碼 `model_not_found` | 模型名稱不在 `models` 清單中。改用 `sonnet`、`opus` 或 `haiku`，或把該名稱加進設定檔 |
| 錯誤代碼 `invalid_json_output` | JSON 模式下，模型連續兩次沒有回傳合法 JSON。可改用更強的模型後重試 |
| QuantDinger 回報連線失敗 | 執行 `./scripts/launchd.sh status` 確認服務在執行。URL 必須用 `host.docker.internal`，不能用 `localhost` |
| 安裝時顯示埠號被占用 | 用 `lsof -nP -iTCP:18790 -sTCP:LISTEN` 找出占用的程式，或在設定檔改用其他埠號 |
| `status` 顯示 service not loaded | 重新執行 `./scripts/launchd.sh install` |

## 開發

```bash
cd tools/claude-cli-bridge
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

測試使用 `tests/fake_claude.py` 模擬 Claude CLI，不會呼叫真正的 Claude，也不會消耗訂閱額度。若要前景執行服務並查看日誌：

```bash
.venv/bin/python -m claude_cli_bridge --config ~/.config/claude-cli-bridge/config.toml --log-level DEBUG
```

前景執行前，請先用 `./scripts/launchd.sh uninstall` 停止常駐服務，否則會因埠號被占用而無法啟動。
