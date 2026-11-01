"""OpenAI-shaped error values raised anywhere in the bridge."""
from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """An error that maps directly onto an OpenAI error response."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 500,
        error_type: str = "server_error",
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_type = error_type
        self.code = code

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "param": None,
            }
        }


def bad_request(message: str, code: str = "invalid_request") -> BridgeError:
    return BridgeError(message, status=400, error_type="invalid_request_error", code=code)


def unauthorized() -> BridgeError:
    return BridgeError(
        "Invalid or missing API key.",
        status=401,
        error_type="authentication_error",
        code="invalid_api_key",
    )
