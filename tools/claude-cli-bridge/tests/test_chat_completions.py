from __future__ import annotations

import os

import pytest
from conftest import WRONG_KEY
from http_client import post_json

from claude_cli_bridge.config import BridgeConfig, ConfigError, load_config
from claude_cli_bridge.errors import BridgeError
from claude_cli_bridge.messages import build_prompt


def chat(base_url: str, messages, **extra):
    return post_json(f"{base_url}/v1/chat/completions", {"model": "sonnet", "messages": messages, **extra})


def test_single_user_message_returns_openai_completion(base_url, calls):
    status, body = chat(base_url, [{"role": "user", "content": "hello"}])
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    choice = body["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "ECHO:hello"}
    assert choice["finish_reason"] == "stop"
    assert body["model"] == "claude-sonnet-test"
    assert body["usage"] == {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
    [call] = calls()
    assert call["stdin"] == "hello"


def test_cli_runs_as_plain_text_model_in_isolated_workdir(base_url, calls, tmp_path):
    chat(base_url, [{"role": "user", "content": "hi"}])
    [call] = calls()
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert call["cwd"].endswith("work")


def test_system_prompt_is_forwarded(base_url, calls):
    chat(base_url, [
        {"role": "system", "content": "Be terse."},
        {"role": "system", "content": "Answer in English."},
        {"role": "user", "content": "q"},
    ])
    assert calls()[0]["system_prompt"] == "Be terse.\n\nAnswer in English."


def test_default_system_prompt_replaces_claude_code_persona(base_url, calls):
    chat(base_url, [{"role": "user", "content": "q"}])
    assert calls()[0]["system_prompt"] == BridgeConfig().default_system_prompt


def test_multi_turn_history_is_rendered_into_prompt(base_url, calls):
    chat(base_url, [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": [{"type": "text", "text": "second question"}]},
    ])
    stdin = calls()[0]["stdin"]
    assert "<user>\nfirst question\n</user>" in stdin
    assert "<assistant>\nfirst answer\n</assistant>" in stdin
    assert stdin.rstrip().endswith("second question")


@pytest.mark.parametrize("api_key", [None, WRONG_KEY])
def test_missing_or_wrong_api_key_is_rejected(base_url, calls, api_key):
    status, body = post_json(
        f"{base_url}/v1/chat/completions",
        {"model": "sonnet", "messages": [{"role": "user", "content": "x"}]},
        api_key=api_key,
    )
    assert status == 401
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["code"] == "invalid_api_key"
    assert calls() == []


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "assistant", "content": "x"}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}],
        [{"role": "wizard", "content": "x"}],
    ],
)
def test_invalid_messages_return_400(base_url, messages):
    status, body = chat(base_url, messages)
    assert status == 400
    assert body["error"]["type"] == "invalid_request_error"


def test_cli_error_result_maps_to_openai_error(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "error")
    status, body = chat(base_url, [{"role": "user", "content": "x"}])
    assert status == 502
    assert body["error"]["message"] == "API Error: something failed"


def test_unparseable_cli_output_maps_to_502(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "garbage")
    status, body = chat(base_url, [{"role": "user", "content": "x"}])
    assert status == 502


def test_unknown_route_returns_404(base_url):
    status, body = post_json(f"{base_url}/v1/nope", {})
    assert status == 404


def test_build_prompt_requires_trailing_user_turn():
    with pytest.raises(BridgeError):
        build_prompt([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])


def test_config_requires_strong_api_key():
    with pytest.raises(ConfigError):
        BridgeConfig(api_key="short").validate()


def test_config_env_overrides_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('port = 9000\napi_key = "' + 'f' * 20 + '"\n')
    config = load_config(path, env={"CLAUDE_BRIDGE_PORT": "9100"})
    assert config.port == 9100
    assert config.api_key == "f" * 20


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("bogus = 1\n")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_request_text_never_reaches_the_command_line(base_url, calls):
    secret = "--tools default PRIVATE-SYSTEM-TEXT"
    chat(base_url, [{"role": "system", "content": secret}, {"role": "user", "content": "q"}])
    call = calls()[0]
    assert call["system_prompt"] == secret
    assert not any("PRIVATE-SYSTEM-TEXT" in arg for arg in call["argv"])
    assert "--system-prompt-file" in call["argv"]


def test_system_prompt_file_is_private_and_removed(base_url, calls):
    chat(base_url, [{"role": "user", "content": "q"}])
    path = calls()[0]["system_prompt_file"]
    assert not os.path.exists(path)
    assert os.path.basename(os.path.dirname(path)) == "prompts"
