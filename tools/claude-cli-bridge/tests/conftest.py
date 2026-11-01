from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest

from claude_cli_bridge.config import BridgeConfig
from claude_cli_bridge.server import BridgeServer

# Deliberately low-entropy so secret scanners do not mistake it for a real key.
API_KEY = "t" * 24
WRONG_KEY = "w" * 24
FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable that runs the fake CLI with the current interpreter."""
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    record = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "echo")
    return wrapper


@pytest.fixture
def calls(tmp_path: Path):
    def read() -> list[dict[str, Any]]:
        path = tmp_path / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    return read


@pytest.fixture
def make_server(fake_cli: Path, tmp_path: Path):
    servers: list[BridgeServer] = []

    def start(**overrides: Any) -> str:
        config = BridgeConfig(
            host="127.0.0.1",
            port=0,
            api_key=API_KEY,
            claude_path=str(fake_cli),
            workdir=str(tmp_path / "work"),
        )
        server = BridgeServer(replace(config, **overrides))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def base_url(make_server) -> Iterator[str]:
    yield make_server()
