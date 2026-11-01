from __future__ import annotations

import http.client
import json
import os
import time
from urllib.parse import urlparse

from conftest import API_KEY


def open_stream(base_url: str, payload: dict) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    parsed = urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
    conn.request(
        "POST",
        "/v1/chat/completions",
        body=json.dumps(payload),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    return conn, conn.getresponse()


def read_events(resp: http.client.HTTPResponse) -> list[str]:
    raw = resp.read().decode()
    return [block[len("data: "):] for block in raw.split("\n\n") if block.startswith("data: ")]


def stream_payload(text: str = "hello") -> dict:
    return {"model": "sonnet", "stream": True, "messages": [{"role": "user", "content": text}]}


def test_stream_emits_openai_chunks_and_done(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TEXT", "abcdefghij")
    conn, resp = open_stream(base_url, stream_payload())
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    events = read_events(resp)
    conn.close()
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert len({c["id"] for c in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "abcdefghij"
    assert [c["choices"][0]["finish_reason"] for c in chunks][-1] == "stop"
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks[:-1])
    assert chunks[-1]["usage"]["total_tokens"] == 10


def test_stream_uses_stream_json_cli_mode(base_url, calls):
    conn, resp = open_stream(base_url, stream_payload())
    read_events(resp)
    conn.close()
    argv = calls()[0]["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv


def test_stream_failure_before_output_returns_json_error(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "error")
    conn, resp = open_stream(base_url, stream_payload())
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 502
    assert body["error"]["message"] == "API Error: something failed"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_client_disconnect_terminates_cli_process(base_url, calls, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TEXT", "x" * 400)
    monkeypatch.setenv("FAKE_CLAUDE_CHUNK_DELAY", "0.2")
    conn, resp = open_stream(base_url, stream_payload())
    assert resp.status == 200
    resp.fp.readline()  # first SSE line arrives
    deadline = time.time() + 5
    while not calls() and time.time() < deadline:
        time.sleep(0.05)
    pid = calls()[0]["pid"]
    assert _pid_alive(pid)
    resp.close()
    conn.close()
    deadline = time.time() + 10
    while _pid_alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid), "CLI process kept running after the client disconnected"
