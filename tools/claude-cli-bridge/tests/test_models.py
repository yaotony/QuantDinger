from __future__ import annotations

import json

import pytest
from conftest import WRONG_KEY
from http_client import post_json, request

from claude_cli_bridge.config import BridgeConfig, ConfigError
from claude_cli_bridge.errors import BridgeError
from claude_cli_bridge.models import resolve


def get_json(url: str, **kwargs):
    status, _, body = request("GET", url, **kwargs)
    return status, json.loads(body)


def test_model_list_is_openai_shaped(base_url):
    status, body = get_json(f"{base_url}/v1/models")
    assert status == 200
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["sonnet", "opus", "haiku"]
    assert all(m["object"] == "model" and m["owned_by"] == "anthropic" for m in body["data"])


def test_model_list_requires_api_key(base_url):
    status, body = get_json(f"{base_url}/v1/models", api_key=WRONG_KEY)
    assert status == 401


def test_retrieve_single_model(base_url):
    assert get_json(f"{base_url}/v1/models/opus")[0] == 200
    status, body = get_json(f"{base_url}/v1/models/gpt-5")
    assert status == 404
    assert body["error"]["code"] == "model_not_found"


def test_configured_aliases_are_listed(make_server):
    url = make_server(models=("opus",), default_model="opus")
    status, body = get_json(f"{url}/v1/models")
    assert [m["id"] for m in body["data"]] == ["opus"]


@pytest.mark.parametrize(
    ("requested", "cli_model"),
    [
        ("opus", "opus"),
        ("anthropic/haiku", "haiku"),
        ("", "sonnet"),
    ],
)
def test_requested_model_reaches_cli(base_url, calls, requested, cli_model):
    status, body = post_json(
        f"{base_url}/v1/chat/completions",
        {"model": requested, "messages": [{"role": "user", "content": "q"}]},
    )
    assert status == 200
    argv = calls()[0]["argv"]
    assert f"--model={cli_model}" in argv
    assert body["model"] == f"claude-{cli_model}-test"


def test_unknown_model_errors_by_default(base_url, calls):
    status, body = post_json(
        f"{base_url}/v1/chat/completions",
        {"model": "gpt-5", "messages": [{"role": "user", "content": "q"}]},
    )
    assert status == 404
    assert body["error"]["code"] == "model_not_found"
    assert calls() == []


def test_unknown_model_can_fall_back_to_default(make_server, calls):
    url = make_server(unknown_model="default", default_model="haiku")
    status, _ = post_json(
        f"{url}/v1/chat/completions",
        {"model": "gpt-5", "messages": [{"role": "user", "content": "q"}]},
    )
    assert status == 200
    argv = calls()[0]["argv"]
    assert "--model=haiku" in argv


def test_full_ids_are_rejected_unless_configured():
    with pytest.raises(BridgeError):
        resolve("claude-opus-5-5", BridgeConfig())
    pinned = BridgeConfig(models=("sonnet", "claude-opus-5-5"))
    assert resolve("claude-opus-5-5", pinned) == "claude-opus-5-5"
    assert resolve("anthropic/claude-opus-5-5", pinned) == "claude-opus-5-5"


def test_configured_full_id_reaches_cli(make_server, calls):
    url = make_server(models=("sonnet", "claude-sonnet-5-5"))
    status, _ = post_json(
        f"{url}/v1/chat/completions",
        {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "q"}]},
    )
    assert status == 200
    assert "--model=claude-sonnet-5-5" in calls()[0]["argv"]


@pytest.mark.parametrize(
    "overrides",
    [{"models": ()}, {"unknown_model": "maybe"}, {"default_model": "gpt-5"}],
)
def test_invalid_model_config_is_rejected(overrides):
    with pytest.raises(ConfigError):
        BridgeConfig(api_key="k" * 20, **overrides).validate()


@pytest.mark.parametrize(
    "name",
    ["claude-opus --tools default", "claude-", "claude-UPPER", "claude-x;rm -rf /", "claude-" + "a" * 80],
)
def test_malformed_claude_ids_are_rejected(base_url, calls, name):
    status, body = post_json(
        f"{base_url}/v1/chat/completions",
        {"model": name, "messages": [{"role": "user", "content": "q"}]},
    )
    assert status == 404
    assert calls() == []
