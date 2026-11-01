from __future__ import annotations

import subprocess
from pathlib import Path

from claude_cli_bridge.config import BridgeConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads_and_validates(tmp_path):
    text = (ROOT / "config.example.toml").read_text()
    assert "REPLACE_WITH_A_RANDOM_SECRET" in text
    filled = text.replace("REPLACE_WITH_A_RANDOM_SECRET", "x" * 32)
    path = tmp_path / "config.toml"
    path.write_text(filled)
    config = load_config(path, env={}).validate()
    defaults = BridgeConfig()
    # The example documents the real defaults, apart from the secret and CLI path.
    for name in ("host", "port", "models", "default_model", "max_concurrency", "overflow",
                 "queue_timeout_seconds", "request_timeout_seconds", "unknown_model"):
        assert getattr(config, name) == getattr(defaults, name), name


def test_launchd_script_is_valid_bash():
    script = ROOT / "scripts" / "launchd.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert script.stat().st_mode & 0o111
