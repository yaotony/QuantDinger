"""Manual verification helper: ``python -m fubon_sidecar.probe '/v1/stock/quote?symbol=2330'``.

Runs inside the sidecar container, reads the token from the environment and prints the
status code and JSON body. The token itself is never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from .config import TOKEN_HEADER


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "/health"
    if not path.startswith("/"):
        print("usage: python -m fubon_sidecar.probe '/v1/...'", file=sys.stderr)
        return 2
    port = os.environ.get("FUBON_SIDECAR_PORT", "8010")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={TOKEN_HEADER: os.environ.get("FUBON_SIDECAR_TOKEN", "")},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed local URL
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    body = json.loads(raw.decode("utf-8") or "null")
    print(status)
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
