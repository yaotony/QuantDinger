"""Run one non-interactive Claude Code CLI turn."""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .errors import BridgeError


@dataclass
class CliResult:
    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


def isolated_workdir(config: BridgeConfig) -> str:
    """An empty directory so the CLI never discovers a project CLAUDE.md."""
    path = Path(config.workdir) if config.workdir else Path(tempfile.gettempdir()) / "claude-cli-bridge"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def build_command(
    config: BridgeConfig,
    *,
    model: str,
    system_prompt: str | None,
    output_format: str = "json",
) -> list[str]:
    command = [
        config.claude_path,
        "-p",
        "--output-format",
        output_format,
        "--model",
        model,
        "--system-prompt",
        system_prompt or config.default_system_prompt,
        # A plain text model: no tools, MCP servers, hooks, settings, or slash commands.
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if output_format == "stream-json":
        command += ["--verbose", "--include-partial-messages"]
    return command


def usage_from(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage") or {}
    prompt_tokens = sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def model_from(payload: dict[str, Any], fallback: str) -> str:
    model_usage = payload.get("modelUsage") or {}
    if isinstance(model_usage, dict) and model_usage:
        return str(next(iter(model_usage)))
    return fallback


def cli_failure(message: str, stderr: str = "") -> BridgeError:
    detail = (message or "").strip() or (stderr or "").strip() or "Claude CLI failed."
    return BridgeError(detail[:2000], status=502, error_type="api_error", code="cli_error")


def run_once(
    config: BridgeConfig,
    *,
    prompt: str,
    model: str,
    system_prompt: str | None,
) -> CliResult:
    command = build_command(config, model=model, system_prompt=system_prompt)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=isolated_workdir(config),
            check=False,
        )
    except FileNotFoundError as exc:
        raise BridgeError(
            f"Claude CLI not found at {config.claude_path!r}.",
            status=503,
            error_type="api_error",
            code="cli_not_found",
        ) from exc
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        raise cli_failure(completed.stdout, completed.stderr) from None
    if not isinstance(payload, dict) or payload.get("is_error") or completed.returncode != 0:
        message = payload.get("result") if isinstance(payload, dict) else ""
        raise cli_failure(str(message or ""), completed.stderr)
    return CliResult(
        text=str(payload.get("result") or ""),
        model=model_from(payload, model),
        usage=usage_from(payload),
    )
