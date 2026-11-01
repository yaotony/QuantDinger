#!/usr/bin/env bash
# Install, remove, or inspect the Claude CLI Bridge macOS LaunchAgent.
#
#   scripts/launchd.sh install     install or upgrade, then start
#   scripts/launchd.sh uninstall   stop and remove the service (keeps config)
#   scripts/launchd.sh restart     restart the running service
#   scripts/launchd.sh status      show launchd state and /health
set -euo pipefail

LABEL="com.quantdinger.claude-cli-bridge"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${HOME}/.local/share/claude-cli-bridge"
VENV="${APP_DIR}/venv"
CONFIG_DIR="${HOME}/.config/claude-cli-bridge"
CONFIG="${CONFIG_DIR}/config.toml"
LOG_DIR="${HOME}/Library/Logs/claude-cli-bridge"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

die() { echo "error: $*" >&2; exit 1; }

find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$candidate" ]] || continue
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return
    fi
  done
  die "Python 3.11 or newer is required"
}

config_port() {
  "${VENV}/bin/python" - "$CONFIG" <<'PY'
import sys
from claude_cli_bridge.config import load_config
print(load_config(sys.argv[1]).port)
PY
}

write_config() {
  [[ -f "$CONFIG" ]] && { echo "keeping existing config: $CONFIG"; return; }
  local claude key
  claude="$(command -v claude || true)"
  [[ -n "$claude" ]] || die "claude CLI not found on PATH; install Claude Code and log in first"
  key="$("${VENV}/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  mkdir -p "$CONFIG_DIR"
  umask 077
  sed -e "s|REPLACE_WITH_A_RANDOM_SECRET|${key}|" \
      -e "s|/Users/YOUR_NAME/.local/bin/claude|${claude}|" \
      "${SRC_DIR}/config.example.toml" > "$CONFIG"
  chmod 600 "$CONFIG"
  echo "created config: $CONFIG"
}

write_plist() {
  local claude_dir
  claude_dir="$(dirname "$(command -v claude)")"
  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV}/bin/python</string>
    <string>-m</string>
    <string>claude_cli_bridge</string>
    <string>--config</string>
    <string>${CONFIG}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${claude_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>
  <key>WorkingDirectory</key><string>${APP_DIR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
    <key>Crashed</key><true/>
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>${LOG_DIR}/bridge.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/bridge.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$PLIST" >/dev/null
}

stop_service() {
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
}

start_service() {
  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "${DOMAIN}/${LABEL}"
}

wait_healthy() {
  local port url i
  port="$(config_port)"
  url="http://127.0.0.1:${port}/health"
  for i in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "healthy: $url"
      return 0
    fi
    sleep 0.5
  done
  echo "service did not become healthy; recent log:" >&2
  tail -n 20 "${LOG_DIR}/bridge.log" >&2 || true
  return 1
}

cmd_install() {
  local python
  python="$(find_python)"
  mkdir -p "$APP_DIR"
  [[ -x "${VENV}/bin/python" ]] || "$python" -m venv "$VENV"
  # Install a copy (not editable) so the service never depends on this checkout's path.
  "${VENV}/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$SRC_DIR"
  write_config
  write_plist
  stop_service
  start_service
  wait_healthy
  echo
  echo "QuantDinger > 系統設定 > AI/LLM 配置:"
  echo "  LLM 提供者: Custom API (OpenAI-compatible)"
  echo "  API URL:    http://host.docker.internal:$(config_port)/v1"
  echo "  API Key:    see api_key in $CONFIG"
}

cmd_uninstall() {
  stop_service
  rm -f "$PLIST"
  rm -rf "$VENV"
  echo "removed service and virtualenv; config kept at $CONFIG"
}

cmd_restart() {
  [[ -f "$PLIST" ]] || die "not installed; run: $0 install"
  launchctl kickstart -k "${DOMAIN}/${LABEL}"
  wait_healthy
}

cmd_status() {
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    launchctl print "${DOMAIN}/${LABEL}" | grep -E '^\s*(state|pid|last exit code) =' || true
  else
    echo "service not loaded"
  fi
  if [[ -x "${VENV}/bin/python" && -f "$CONFIG" ]]; then
    curl -sS "http://127.0.0.1:$(config_port)/health" || true
    echo
  fi
}

case "${1:-}" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  *) echo "usage: $0 {install|uninstall|restart|status}" >&2; exit 64 ;;
esac
