"""Model aliases exposed by the bridge and their mapping to CLI ``--model`` values."""
from __future__ import annotations

import re
from typing import Any

from .config import BridgeConfig
from .errors import BridgeError

_PROVIDER_PREFIXES = ("anthropic/", "claude-cli/")
_CLAUDE_MODEL_ID = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,63}")


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


def _allowed(name: str, config: BridgeConfig) -> str | None:
    """Return the trusted CLI model value for ``name``, or None if it is not allowed.

    Aliases resolve to the configured string itself, and full ids must match a
    strict pattern, so request text never reaches the CLI unchecked.
    """
    for alias in config.models:
        if name == alias:
            return alias
    if config.allow_claude_model_ids:
        match = _CLAUDE_MODEL_ID.fullmatch(name)
        if match:
            return match.group(0)
    return None


def is_known(name: str, config: BridgeConfig) -> bool:
    return _allowed(name, config) is not None


def resolve(requested: Any, config: BridgeConfig) -> str:
    """Return the CLI model value for a requested OpenAI ``model`` field."""
    name = _strip_prefix(str(requested or "").strip()) or config.default_model
    allowed = _allowed(name, config)
    if allowed is not None:
        return allowed
    if config.unknown_model == "default":
        return config.default_model
    raise model_not_found(name)


def model_card(name: str) -> dict[str, Any]:
    return {"id": name, "object": "model", "created": 0, "owned_by": "anthropic"}


def model_list(config: BridgeConfig) -> dict[str, Any]:
    return {"object": "list", "data": [model_card(name) for name in config.models]}
