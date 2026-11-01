from __future__ import annotations

import json

import pytest
from http_client import post_json
from test_streaming import open_stream, read_events

from claude_cli_bridge.json_mode import JSON_INSTRUCTION, extract_json_object


def json_chat(base_url: str, **extra):
    payload = {
        "model": "sonnet",
        "messages": [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "q"}],
        "response_format": {"type": "json_object"},
        **extra,
    }
    return post_json(f"{base_url}/v1/chat/completions", payload)


def use_replies(monkeypatch, *replies: str) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_TEXTS", json.dumps(list(replies)))


def test_plain_json_reply_passes_through(base_url, calls, monkeypatch):
    use_replies(monkeypatch, '{"signal": "buy", "confidence": 0.7}')
    status, body = json_chat(base_url)
    assert status == 200
    assert json.loads(body["choices"][0]["message"]["content"]) == {"signal": "buy", "confidence": 0.7}
    [call] = calls()
    system = call["argv"][call["argv"].index("--system-prompt") + 1]
    assert system.startswith("Be helpful.")
    assert JSON_INSTRUCTION in system


def test_fenced_json_is_unwrapped(base_url, calls, monkeypatch):
    use_replies(monkeypatch, '```json\n{"a": 1}\n```')
    status, body = json_chat(base_url)
    assert status == 200
    assert body["choices"][0]["message"]["content"] == '{"a": 1}'
    assert len(calls()) == 1


def test_invalid_then_valid_retries_once(base_url, calls, monkeypatch):
    use_replies(monkeypatch, "Sure! Here you go.", '{"ok": true}')
    status, body = json_chat(base_url)
    assert status == 200
    assert json.loads(body["choices"][0]["message"]["content"]) == {"ok": True}
    first, second = calls()
    assert "IMPORTANT" not in first["stdin"]
    assert "IMPORTANT" in second["stdin"]


def test_invalid_twice_returns_error(base_url, calls, monkeypatch):
    use_replies(monkeypatch, "nope", "still nope")
    status, body = json_chat(base_url)
    assert status == 502
    assert body["error"]["code"] == "invalid_json_output"
    assert len(calls()) == 2


def test_text_mode_is_unchanged(base_url, calls, monkeypatch):
    use_replies(monkeypatch, "not json and that is fine")
    status, body = post_json(
        f"{base_url}/v1/chat/completions",
        {
            "model": "sonnet",
            "messages": [{"role": "user", "content": "q"}],
            "response_format": {"type": "text"},
        },
    )
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "not json and that is fine"
    system = calls()[0]["argv"][calls()[0]["argv"].index("--system-prompt") + 1]
    assert JSON_INSTRUCTION not in system


def test_json_mode_with_stream_sends_validated_json(base_url, monkeypatch):
    use_replies(monkeypatch, '```\n{"x": [1, 2]}\n```')
    conn, resp = open_stream(base_url, {
        "model": "sonnet",
        "stream": True,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": "q"}],
    })
    events = read_events(resp)
    conn.close()
    assert events[-1] == "[DONE]"
    text = "".join(json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1])
    assert json.loads(text) == {"x": [1, 2]}


def test_unsupported_response_format_is_rejected(base_url):
    status, body = post_json(
        f"{base_url}/v1/chat/completions",
        {
            "model": "sonnet",
            "messages": [{"role": "user", "content": "q"}],
            "response_format": {"type": "xml"},
        },
    )
    assert status == 400


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  ```json\n{"a": 1}\n```  ', '{"a": 1}'),
        ('Here it is: {"a": {"b": 2}} done', '{"a": {"b": 2}}'),
        ("[1, 2]", None),
        ("plain text", None),
        ("", None),
    ],
)
def test_extract_json_object(reply, expected):
    assert extract_json_object(reply) == expected
