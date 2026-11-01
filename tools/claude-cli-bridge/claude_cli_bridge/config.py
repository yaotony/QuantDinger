"""Bridge configuration loaded from a TOML file with environment overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "claude-cli-bridge" / "config.toml"
ENV_PREFIX = "CLAUDE_BRIDGE_"


class ConfigError(ValueError):
    """Raised when the bridge configuration is unusable."""


@dataclass(frozen=True)
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 18790
    api_key: str = ""
    claude_path: str = "claude"
    default_model: str = "sonnet"
    default_system_prompt: str = "You are a helpful assistant."
    models: tuple[str, ...] = ("sonnet", "opus", "haiku")
    unknown_model: str = "error"
    max_concurrency: int = 2
    overflow: str = "queue"
    queue_timeout_seconds: float = 60.0
    request_timeout_seconds: float = 300.0
    workdir: str = ""
    max_body_bytes: int = 10 * 1024 * 1024

    def validate(self) -> "BridgeConfig":
        if len(self.api_key.strip()) < 16:
            raise ConfigError("api_key must be set to a random value of at least 16 characters")
        if not 0 <= self.port <= 65535:
            raise ConfigError("port must be between 0 and 65535")
        if self.max_concurrency < 1:
            raise ConfigError("max_concurrency must be at least 1")
        if self.overflow not in {"queue", "reject"}:
            raise ConfigError("overflow must be 'queue' or 'reject'")
        if self.queue_timeout_seconds < 0 or self.request_timeout_seconds <= 0:
            raise ConfigError("timeouts must be positive")
        if not self.models:
            raise ConfigError("models must list at least one model alias")
        if self.unknown_model not in {"error", "default"}:
            raise ConfigError("unknown_model must be 'error' or 'default'")
        from .models import is_known

        if not is_known(self.default_model, self):
            raise ConfigError("default_model must be one of models")
        return self


def _coerce(name: str, raw: Any, target: Any) -> Any:
    try:
        if isinstance(target, bool):
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(target, int):
            return int(raw)
        if isinstance(target, float):
            return float(raw)
        if isinstance(target, (list, tuple)):
            if isinstance(raw, str):
                return tuple(item.strip() for item in raw.split(",") if item.strip())
            return tuple(raw)
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value for {name}: {raw!r}") from exc


def load_config(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> BridgeConfig:
    """Load config from TOML (if present) and apply CLAUDE_BRIDGE_* overrides."""
    env = os.environ if env is None else env
    config_path = Path(path or env.get(f"{ENV_PREFIX}CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    values: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("rb") as handle:
            values.update(tomllib.load(handle))
    defaults = BridgeConfig()
    known = {f.name: getattr(defaults, f.name) for f in fields(BridgeConfig)}
    unknown = set(values) - set(known)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    for name in known:
        env_value = env.get(f"{ENV_PREFIX}{name.upper()}")
        if env_value is not None:
            values[name] = env_value
    coerced = {name: _coerce(name, value, known[name]) for name, value in values.items()}
    return BridgeConfig(**coerced)
