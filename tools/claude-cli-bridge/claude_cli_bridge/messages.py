"""Convert OpenAI chat messages into a single Claude CLI prompt."""
from __future__ import annotations

from typing import Any

from .errors import bad_request

_SYSTEM_ROLES = {"system", "developer"}
_TURN_ROLES = {"user", "assistant", "tool"}


def content_text(content: Any) -> str:
    """Flatten OpenAI message content (string or text parts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                raise bad_request("Unsupported message content part.")
            if part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text") or ""))
                continue
            raise bad_request(
                f"Unsupported content part type: {part.get('type')!r}. Only text is supported.",
                code="unsupported_content",
            )
        return "\n".join(parts)
    raise bad_request("Message content must be a string or a list of text parts.")


def build_prompt(messages: Any) -> tuple[str, str | None]:
    """Return ``(prompt, system_prompt)`` for one non-interactive CLI turn."""
    if not isinstance(messages, list) or not messages:
        raise bad_request("'messages' must be a non-empty list.")
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise bad_request("Each message must be an object.")
        role = message.get("role")
        text = content_text(message.get("content"))
        if role in _SYSTEM_ROLES:
            if text.strip():
                system_parts.append(text)
        elif role in _TURN_ROLES:
            turns.append((role, text))
        else:
            raise bad_request(f"Unsupported message role: {role!r}.")
    if not turns or turns[-1][0] != "user":
        raise bad_request("The last non-system message must have role 'user'.")
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    if len(turns) == 1:
        return turns[0][1], system_prompt
    history = "\n\n".join(f"<{role}>\n{text}\n</{role}>" for role, text in turns[:-1])
    prompt = (
        "The following is the earlier part of this conversation.\n"
        f"<conversation_history>\n{history}\n</conversation_history>\n\n"
        "Reply to the user's latest message below, continuing the conversation.\n\n"
        f"{turns[-1][1]}"
    )
    return prompt, system_prompt
