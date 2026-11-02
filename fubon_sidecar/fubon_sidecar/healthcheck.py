"""Container healthcheck: ``python -m fubon_sidecar.healthcheck``.

Exits 0 only when the sidecar answers ``/health`` with ``logged_in`` and
``realtime_ready`` both true. Uses the stdlib only and never prints the token.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from .config import TOKEN_HEADER


def check(url: str, token: str, timeout: float = 5.0) -> bool:
    request = urllib.request.Request(url, headers={TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed local URL
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck failed: {type(exc).__name__}", file=sys.stderr)
        return False
    ready = bool(body.get("logged_in")) and bool(body.get("realtime_ready"))
    if not ready:
        print(f"healthcheck not ready: status={body.get('status')}", file=sys.stderr)
    return ready


def main() -> int:
    port = os.environ.get("FUBON_SIDECAR_PORT", "8010")
    token = os.environ.get("FUBON_SIDECAR_TOKEN", "")
    return 0 if check(f"http://127.0.0.1:{port}/health", token) else 1


if __name__ == "__main__":
    sys.exit(main())
