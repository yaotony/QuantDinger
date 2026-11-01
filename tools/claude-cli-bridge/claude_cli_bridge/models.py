"""Model aliases exposed by the bridge and their mapping to CLI ``--model`` values."""
from __future__ import annotations

from typing import Any

from .config import BridgeConfig
from .errors import BridgeError

_PROVIDER_PREFIXES = ("anthropic/", "claude-cli/")


def model_not_found(name: str) -> BridgeError:
    return BridgeError(
        f"The model {name!r} does not exist or is not enabled on this bridge.",
        status=404,
        error_type="invalid_request_error",
        code="model_not_found",
    )


def _strip_prefix(name: str) -> str:
    for prefix in _PROVIDER_PREFIXES:
        if name.lower().startswith(prefix):
            return name[len(prefix):]
    return name


def is_known(name: str, config: BridgeConfig) -> bool:
    if name in config.models:
        return True
    return config.allow_claude_model_ids and name.startswith("claude-")


def resolve(requested: Any, config: BridgeConfig) -> str:
    """Return the CLI model value for a requested OpenAI ``model`` field."""
    name = _strip_prefix(str(requested or "").strip()) or config.default_model
    if is_known(name, config):
        return name
    if config.unknown_model == "default":
        return config.default_model
    raise model_not_found(name)


def model_card(name: str) -> dict[str, Any]:
    return {"id": name, "object": "model", "created": 0, "owned_by": "anthropic"}


def model_list(config: BridgeConfig) -> dict[str, Any]:
    return {"object": "list", "data": [model_card(name) for name in config.models]}
