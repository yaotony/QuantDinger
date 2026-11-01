"""Run one non-interactive Claude Code CLI turn."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
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


_NOT_AUTHENTICATED = (
    "not logged in",
    "please run /login",
    "/login",
    "invalid api key",
    "oauth token",
    "authentication_error",
    "credit balance",
)
_USAGE_LIMIT = ("usage limit", "limit reached", "rate limit", "rate_limit", "too many requests")
_OVERLOADED = ("overloaded", "529")


def cli_failure(message: str, stderr: str = "") -> BridgeError:
    """Translate a failed CLI turn into a specific OpenAI-shaped error."""
    detail = (message or "").strip() or (stderr or "").strip() or "Claude CLI failed."
    detail = detail[:2000]
    haystack = f"{message}\n{stderr}".lower()
    if "unrecognized_model" in haystack or "model not found" in haystack:
        return BridgeError(
            f"The Claude CLI does not recognise this model: {detail}",
            status=404,
            error_type="invalid_request_error",
            code="model_not_found",
        )
    if any(marker in haystack for marker in _NOT_AUTHENTICATED):
        return BridgeError(
            f"The Claude CLI on the bridge host is not logged in or its login expired. "
            f"Run `claude` on that machine and log in again. Detail: {detail}",
            status=503,
            error_type="api_error",
            code="cli_not_authenticated",
        )
    if any(marker in haystack for marker in _USAGE_LIMIT):
        return BridgeError(
            f"The Claude subscription usage limit was reached. Detail: {detail}",
            status=429,
            error_type="rate_limit_error",
            code="cli_usage_limit",
        )
    if any(marker in haystack for marker in _OVERLOADED):
        return BridgeError(
            f"Claude is temporarily overloaded. Detail: {detail}",
            status=503,
            error_type="api_error",
            code="upstream_overloaded",
        )
    return BridgeError(detail, status=502, error_type="api_error", code="cli_error")


def cli_timeout(seconds: float) -> BridgeError:
    return BridgeError(
        f"The Claude CLI did not finish within {seconds:g} seconds.",
        status=504,
        error_type="api_error",
        code="cli_timeout",
    )


def cli_not_found(path: str) -> BridgeError:
    return BridgeError(
        f"Claude CLI not found at {path!r}.",
        status=503,
        error_type="api_error",
        code="cli_not_found",
    )


def run_once(
    config: BridgeConfig,
    *,
    prompt: str,
    model: str,
    system_prompt: str | None,
) -> CliResult:
    command = build_command(config, model=model, system_prompt=system_prompt)
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=isolated_workdir(config),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise cli_not_found(config.claude_path) from exc
    try:
        stdout, stderr = proc.communicate(prompt, timeout=config.request_timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate(proc)
        raise cli_timeout(config.request_timeout_seconds) from None
    except BaseException:
        terminate(proc)
        raise
    completed = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
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


class CliStream:
    """Iterate text deltas from ``claude -p --output-format stream-json``.

    After iteration finishes, ``result`` holds the final CliResult. Call
    ``close()`` to terminate the CLI early (for example on client disconnect).
    """

    def __init__(
        self,
        config: BridgeConfig,
        *,
        prompt: str,
        model: str,
        system_prompt: str | None,
    ) -> None:
        self.model = model
        self.result: CliResult | None = None
        command = build_command(config, model=model, system_prompt=system_prompt, output_format="stream-json")
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=isolated_workdir(config),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise cli_not_found(config.claude_path) from exc
        self.timed_out = False
        self._timeout = config.request_timeout_seconds
        self._watchdog = threading.Timer(self._timeout, self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(prompt)
            self._proc.stdin.close()
        except BrokenPipeError:
            pass

    @property
    def pid(self) -> int:
        return self._proc.pid

    def __iter__(self):
        assert self._proc.stdout is not None
        final: dict[str, Any] | None = None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "stream_event":
                inner = event.get("event") or {}
                delta = inner.get("delta") or {}
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if text:
                        yield text
            elif kind == "result":
                final = event
        returncode = self._proc.wait()
        self._watchdog.cancel()
        stderr = self._proc.stderr.read() if self._proc.stderr else ""
        if self.timed_out:
            raise cli_timeout(self._timeout)
        if final is None or final.get("is_error") or returncode != 0:
            message = (final or {}).get("result") or ""
            raise cli_failure(str(message), stderr)
        self.result = CliResult(
            text=str(final.get("result") or ""),
            model=model_from(final, self.model),
            usage=usage_from(final),
        )

    def _expire(self) -> None:
        self.timed_out = True
        terminate(self._proc)

    def close(self) -> None:
        self._watchdog.cancel()
        terminate(self._proc)


def terminate(proc: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    """Stop a CLI process group, escalating from SIGTERM to SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait()
