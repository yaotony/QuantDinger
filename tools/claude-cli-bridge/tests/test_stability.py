from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest
from conftest import API_KEY
from http_client import post_json, request
from test_streaming import open_stream, read_events

from claude_cli_bridge.cli import cli_failure
from claude_cli_bridge.config import BridgeConfig, ConfigError


def chat(url: str, **extra):
    return post_json(
        f"{url}/v1/chat/completions",
        {"model": "sonnet", "messages": [{"role": "user", "content": "q"}], **extra},
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_cli_and_returns_504(make_server, calls, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "sleep")
    url = make_server(request_timeout_seconds=1)
    started = time.monotonic()
    status, body = chat(url)
    assert status == 504
    assert body["error"]["code"] == "cli_timeout"
    assert time.monotonic() - started < 8
    pid = calls()[0]["pid"]
    deadline = time.time() + 5
    while _pid_alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid)


def test_stream_timeout_kills_cli(make_server, calls, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TEXT", "y" * 400)
    monkeypatch.setenv("FAKE_CLAUDE_CHUNK_DELAY", "0.2")
    url = make_server(request_timeout_seconds=1)
    payload = {"model": "sonnet", "stream": True, "messages": [{"role": "user", "content": "q"}]}
    conn, resp = open_stream(url, payload)
    events = read_events(resp)
    conn.close()
    assert events[-1] == "[DONE]"
    error = json.loads(events[-2])
    assert error["error"]["code"] == "cli_timeout"
    assert not _pid_alive(calls()[0]["pid"])


def _hold_slot(url: str, results: list) -> threading.Thread:
    thread = threading.Thread(target=lambda: results.append(chat(url)), daemon=True)
    thread.start()
    return thread


def _wait_in_flight(url: str, count: int) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        _, _, body = request("GET", f"{url}/health", api_key=None)
        if json.loads(body)["in_flight"] >= count:
            return
        time.sleep(0.05)
    raise AssertionError("slot was never taken")


def test_reject_overflow_returns_429(make_server, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "sleep")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")
    url = make_server(max_concurrency=1, overflow="reject")
    results: list = []
    holder = _hold_slot(url, results)
    _wait_in_flight(url, 1)
    status, body = chat(url)
    assert status == 429
    assert body["error"]["code"] == "bridge_busy"
    holder.join(10)


def test_queue_overflow_waits_for_a_free_slot(make_server, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "sleep")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "1")
    url = make_server(max_concurrency=1, overflow="queue", queue_timeout_seconds=10)
    results: list = []
    holder = _hold_slot(url, results)
    _wait_in_flight(url, 1)
    status, _ = chat(url)
    holder.join(10)
    assert status == 200
    assert results[0][0] == 200


def test_queue_timeout_returns_429(make_server, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "sleep")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "3")
    url = make_server(max_concurrency=1, overflow="queue", queue_timeout_seconds=0.5)
    results: list = []
    holder = _hold_slot(url, results)
    _wait_in_flight(url, 1)
    status, body = chat(url)
    assert status == 429
    holder.join(10)


@pytest.mark.parametrize(
    ("message", "stderr", "status", "code"),
    [
        ("", "[claude-code:unrecognized_model] {}", 404, "model_not_found"),
        ("Not logged in · Please run /login", "", 503, "cli_not_authenticated"),
        ("Claude AI usage limit reached|1793520000", "", 429, "cli_usage_limit"),
        ("API Error: 529 Overloaded", "", 503, "upstream_overloaded"),
        ("API Error: 500 something", "", 502, "cli_error"),
        ("", "", 502, "cli_error"),
    ],
)
def test_cli_failures_are_classified(message, stderr, status, code):
    error = cli_failure(message, stderr)
    assert (error.status, error.code) == (status, code)


def test_cli_stderr_failure_reaches_client(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "stderr_error")
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "Not logged in · Please run /login")
    status, body = chat(base_url)
    assert status == 503
    assert body["error"]["code"] == "cli_not_authenticated"
    assert "log in" in body["error"]["message"]


def test_nonzero_exit_without_json_is_an_error(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    status, body = chat(base_url)
    assert status == 502
    assert "segfault-ish" in body["error"]["message"]


def test_missing_cli_is_reported(make_server):
    url = make_server(claude_path="/nonexistent/claude")
    status, body = chat(url)
    assert status == 503
    assert body["error"]["code"] == "cli_not_found"
    status, _, raw = request("GET", f"{url}/health", api_key=None)
    assert status == 503
    assert json.loads(raw)["status"] == "degraded"


def test_health_is_public_and_reports_capacity(make_server):
    url = make_server(max_concurrency=3)
    status, headers, raw = request("GET", f"{url}/health", api_key=None)
    body = json.loads(raw)
    assert status == 200
    assert body["status"] == "ok"
    assert body["max_concurrency"] == 3
    assert body["in_flight"] == 0
    assert headers.get("X-Request-Id")


def test_request_log_has_metadata_but_no_prompt_or_key(base_url, caplog, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TEXT", "SECRET-COMPLETION")
    with caplog.at_level(logging.INFO, logger="claude_cli_bridge"):
        post_json(
            f"{base_url}/v1/chat/completions",
            {"model": "haiku", "messages": [{"role": "user", "content": "SECRET-PROMPT"}]},
        )
    records = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    entry = next(r for r in records if r["path"] == "/v1/chat/completions")
    assert entry["status"] == 200
    assert entry["model"] == "haiku"
    assert entry["stream"] is False
    assert isinstance(entry["duration_ms"], int)
    text = caplog.text
    assert "SECRET-PROMPT" not in text
    assert "SECRET-COMPLETION" not in text
    assert API_KEY not in text


@pytest.mark.parametrize(
    "overrides",
    [{"max_concurrency": 0}, {"overflow": "drop"}, {"request_timeout_seconds": 0}],
)
def test_invalid_stability_config_is_rejected(overrides):
    with pytest.raises(ConfigError):
        BridgeConfig(api_key="k" * 20, **overrides).validate()
