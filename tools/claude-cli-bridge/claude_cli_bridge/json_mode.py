"""Emulate OpenAI ``response_format`` JSON modes on top of the Claude CLI."""
from __future__ import annotations

import json
import re
from typing import Any

from . import cli
from .config import BridgeConfig
from .errors import BridgeError, bad_request

JSON_INSTRUCTION = (
    "Output format requirement: respond with a single valid JSON object and nothing else. "
    "Do not wrap it in Markdown code fences and do not add any text before or after it."
)
RETRY_REMINDER = (
    "\n\nIMPORTANT: your reply must be one valid JSON object only, with no Markdown "
    "fences and no surrounding prose."
)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)


def requested(response_format: Any) -> bool:
    """True when the request asks for JSON output."""
    if response_format in (None, {}):
        return False
    if not isinstance(response_format, dict):
        raise bad_request("'response_format' must be an object.")
    kind = response_format.get("type")
    if kind in (None, "text"):
        return False
    if kind in ("json_object", "json_schema"):
        return True
    raise bad_request(f"Unsupported response_format type: {kind!r}.")


def extract_json_object(text: str) -> str | None:
    """Return the JSON object text contained in a model reply, or None."""
    candidate = (text or "").strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group("body").strip()
    for attempt in (candidate, _outer_braces(candidate)):
        if not attempt:
            continue
        try:
            value = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return attempt
    return None


def _outer_braces(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def with_instruction(system_prompt: str | None, config: BridgeConfig) -> str:
    base = system_prompt or config.default_system_prompt
    return f"{base}\n\n{JSON_INSTRUCTION}"


def run(
    config: BridgeConfig,
    *,
    prompt: str,
    model: str,
    system_prompt: str | None,
) -> cli.CliResult:
    """Run a JSON-mode turn, retrying once when the reply is not a JSON object."""
    system = with_instruction(system_prompt, config)
    result = cli.run_once(config, prompt=prompt, model=model, system_prompt=system)
    extracted = extract_json_object(result.text)
    if extracted is None:
        result = cli.run_once(config, prompt=prompt + RETRY_REMINDER, model=model, system_prompt=system)
        extracted = extract_json_object(result.text)
    if extracted is None:
        raise BridgeError(
            "The model did not return a valid JSON object after one retry.",
            status=502,
            error_type="api_error",
            code="invalid_json_output",
        )
    result.text = extracted
    return result
