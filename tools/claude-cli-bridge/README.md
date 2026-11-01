# Claude CLI Bridge

在本機提供 OpenAI 相容的 `/v1/chat/completions` 端點，內部呼叫 Claude Code CLI（`claude -p`），讓 QuantDinger 的「Custom API」LLM 提供者可以使用本機已登入的 Claude。

完整的安裝與設定說明會在後續版本補上。

## 快速啟動

```bash
cd tools/claude-cli-bridge
python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p ~/.config/claude-cli-bridge
cat > ~/.config/claude-cli-bridge/config.toml <<'TOML'
api_key = "請換成至少 16 字元的隨機字串"
claude_path = "/Users/<you>/.local/bin/claude"
TOML
.venv/bin/python -m claude_cli_bridge
```

服務預設只監聽 `127.0.0.1:18790`。QuantDinger 在 Docker 內，請把 Custom API URL 設為 `http://host.docker.internal:18790/v1`。
