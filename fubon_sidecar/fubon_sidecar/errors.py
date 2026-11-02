"""Errors that map one-to-one onto the sidecar HTTP error contract."""

from __future__ import annotations


class SidecarError(Exception):
    status_code = 500
    code = "internalError"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message

    def to_payload(self) -> dict[str, str]:
        payload = {"code": self.code}
        if self.message:
            payload["message"] = self.message
        return payload


class InvalidRequestError(SidecarError):
    status_code = 400
    code = "invalidRequest"


class UnauthorizedError(SidecarError):
    status_code = 401
    code = "unauthorized"


class NotFoundError(SidecarError):
    status_code = 404
    code = "notFound"


class RateLimitedError(SidecarError):
    status_code = 429
    code = "rateLimited"


class UpstreamError(SidecarError):
    status_code = 502
    code = "upstreamError"


class NotReadyError(SidecarError):
    status_code = 503
    code = "notReady"
