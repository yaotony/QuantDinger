"""Environment-driven settings for the Fubon market data sidecar."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

TOKEN_HEADER = "X-Sidecar-Token"
MIN_TOKEN_LENGTH = 32
DEFAULT_PORT = 8010
DEFAULT_TIMEOUT_SEC = 10.0
DEFAULT_RATE_LIMIT_PER_SEC = 5.0


class ConfigError(ValueError):
    """Raised when the sidecar configuration is unusable."""


def _float(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ConfigError(f"{key} must be a valid TCP port")
    return value


@dataclass(frozen=True)
class Settings:
    """Sidecar settings. Secret fields are excluded from repr so they never reach logs."""

    sidecar_token: str = field(repr=False)
    fubon_id: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    cert_path: str = field(default="", repr=False)
    cert_password: str = field(default="", repr=False)
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    request_timeout_sec: float = DEFAULT_TIMEOUT_SEC
    rate_limit_per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC

    def __post_init__(self) -> None:
        if len(self.sidecar_token or "") < MIN_TOKEN_LENGTH:
            raise ConfigError(f"FUBON_SIDECAR_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")

    @property
    def uses_api_key(self) -> bool:
        return bool(self.api_key)

    def missing_credentials(self) -> list[str]:
        """Names (never values) of the login variables that are not set."""
        required = {
            "FUBON_ID": self.fubon_id,
            "FUBON_CERT_PATH": self.cert_path,
            "FUBON_CERT_PASSWORD": self.cert_password,
        }
        if not self.uses_api_key:
            required["FUBON_PASSWORD"] = self.password
        return [name for name, value in required.items() if not value]

    def secret_values(self) -> list[str]:
        """Values that must be scrubbed from any log line or error message."""
        values = [
            self.sidecar_token,
            self.fubon_id,
            self.password,
            self.api_key,
            self.cert_password,
        ]
        return [value for value in values if value]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            sidecar_token=(env.get("FUBON_SIDECAR_TOKEN") or "").strip(),
            fubon_id=(env.get("FUBON_ID") or "").strip(),
            password=env.get("FUBON_PASSWORD") or "",
            api_key=(env.get("FUBON_API_KEY") or "").strip(),
            cert_path=(env.get("FUBON_CERT_PATH") or "").strip(),
            cert_password=env.get("FUBON_CERT_PASSWORD") or "",
            host=(env.get("FUBON_SIDECAR_HOST") or "0.0.0.0").strip(),
            port=_int(env, "FUBON_SIDECAR_PORT", DEFAULT_PORT),
            request_timeout_sec=_float(env, "FUBON_REQUEST_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC, minimum=0.1),
            rate_limit_per_sec=_float(
                env, "FUBON_RATE_LIMIT_PER_SEC", DEFAULT_RATE_LIMIT_PER_SEC, minimum=0.1
            ),
        )
